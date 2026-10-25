"""Streamlit control room for live chat, poll control, and community memory.

This UI does not ban, mute, time out, or delete YouTube users/comments. Polls are
created/closed only when the owner presses the corresponding dashboard action.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape

import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import func, select

from youtube_bot_dashboard.community_memory import (
    CommunityMemory,
    CommunityViewerProfile,
    forget_saved_memory,
    forget_viewer,
    review_memory_suggestion,
)
from youtube_bot_dashboard.chat_visibility import is_ai_whitelisted_for_display
from youtube_bot_dashboard.live_queue import (
    BatchState,
    LiveAIRequestBatch,
    LiveSessionRecord,
    SessionShieldRecord,
    LiveMessageRecord,
    LiveMessageTask,
    LivePipeline,
    LiveQueueConfig,
    ProviderName,
    ProviderStrategy,
    TaskState,
    get_live_queue_config,
    unshield_user_for_session,
    update_live_queue_config,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema
from youtube_bot_dashboard.live_queue import LIVE_MESSAGE_TEXT_EXPIRED
from youtube_bot_dashboard.message_retention import (
    get_live_message_retention_days,
    save_live_message_retention_days,
    run_live_message_retention_cleanup,
)
from youtube_bot_dashboard.providers.registry import ProviderRegistry
from youtube_bot_dashboard.provider_analytics import (
    PRICE_FIELDS,
    ProviderRateCard,
    get_provider_analytics,
    load_provider_rate_card,
    save_provider_rate_card,
)
from youtube_bot_dashboard.live_polls import (
    POLL_ACTIVE,
    POLL_CLOSED,
    POLL_DRAFT,
    LivePollRecord,
    create_poll_draft,
    poll_options,
)
from youtube_bot_dashboard.youtube_polls import end_poll_on_youtube, launch_poll_on_youtube


load_dotenv()

st.set_page_config(
    page_title="YouTube Bot Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_database():
    database_url = os.environ.get("DATABASE_URL", "sqlite:///./youtube_bot_dashboard.db")
    engine, sessions = create_engine_and_sessions(database_url)
    create_schema(engine)
    return engine, sessions


engine, SessionFactory = get_database()
provider_registry = ProviderRegistry.from_environment()


@contextmanager
def db_session():
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()


def super_chat_amount_label(message) -> str:
    """Prefer YouTube's display string; otherwise format the source amount."""
    if message.super_chat_amount_display:
        return message.super_chat_amount_display
    if message.super_chat_amount_micros is not None:
        amount = f"{message.super_chat_amount_micros / 1_000_000:,.2f}"
        return f"{amount} {message.super_chat_currency or ''}".strip()
    return "Amount unavailable"


def read_counts(session):
    total_messages = session.scalar(select(func.count()).select_from(LiveMessageRecord)) or 0
    total_tasks = session.scalar(select(func.count()).select_from(LiveMessageTask)) or 0
    pending = session.scalar(
        select(func.count()).select_from(LiveMessageTask).where(
            LiveMessageTask.state.in_([
                TaskState.PENDING.value,
                TaskState.BATCHED.value,
                TaskState.SENT.value,
                TaskState.FAILED_RETRYABLE.value,
            ])
        )
    ) or 0
    total_batches = session.scalar(select(func.count()).select_from(LiveAIRequestBatch)) or 0
    return total_messages, total_tasks, pending, total_batches



def _task_result(task):
    if task is None:
        return {}
    try:
        result = json.loads(task.result_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def load_dashboard_snapshot():
    """Load a real, safe dashboard snapshot; hidden message bodies never enter hidden panels."""
    with db_session() as session:
        total_messages, total_tasks, pending_tasks, total_batches = read_counts(session)
        active_sessions = list(session.scalars(
            select(LiveSessionRecord)
            .where(LiveSessionRecord.status == "ACTIVE")
            .order_by(LiveSessionRecord.started_at.desc())
        ))
        session_ids = [row.session_id for row in active_sessions]
        if not session_ids:
            latest_id = session.scalar(
                select(LiveMessageRecord.session_id)
                .order_by(LiveMessageRecord.last_seen_at.desc())
                .limit(1)
            )
            session_ids = [latest_id] if latest_id else []

        message_query = select(LiveMessageRecord)
        if session_ids:
            message_query = message_query.where(LiveMessageRecord.session_id.in_(session_ids))
        else:
            message_query = message_query.where(LiveMessageRecord.message_id == "__no_messages__")
        messages = list(session.scalars(
            message_query.order_by(
                LiveMessageRecord.is_super_chat.desc(),
                LiveMessageRecord.first_seen_at.desc(),
            ).limit(250)
        ))
        tasks_by_key = {}
        if messages:
            tasks = list(session.scalars(
                select(LiveMessageTask).where(
                    LiveMessageTask.message_id.in_([message.message_id for message in messages])
                )
            ))
            tasks_by_key = {(task.message_id, task.pipeline): task for task in tasks}

        active_shield_keys = {
            (row[0], row[1])
            for row in session.execute(
                select(SessionShieldRecord.session_id, SessionShieldRecord.user_channel_id)
                .join(LiveSessionRecord, LiveSessionRecord.session_id == SessionShieldRecord.session_id)
                .where(
                    SessionShieldRecord.active.is_(True),
                    LiveSessionRecord.status == "ACTIVE",
                )
            ).all()
        }

        feed_items = []
        for message in messages:
            moderation_task = tasks_by_key.get((message.message_id, LivePipeline.MODERATION.value))
            reply_task = tasks_by_key.get((message.message_id, LivePipeline.REPLY_FINDER.value))
            moderation_result = _task_result(moderation_task)
            reply_result = _task_result(reply_task)
            is_shielded = bool(
                message.user_channel_id
                and (message.session_id, message.user_channel_id) in active_shield_keys
            )
            content_expired = message.text == LIVE_MESSAGE_TEXT_EXPIRED
            approved, reason = is_ai_whitelisted_for_display(
                moderation_state=moderation_task.state if moderation_task else None,
                moderation_result=moderation_result,
                reply_finder_state=reply_task.state if reply_task else None,
                reply_finder_result=reply_result,
                is_super_chat=message.is_super_chat,
            )
            if content_expired:
                approved = False
                reason = "Message body removed by the live-message retention policy."
            if is_shielded:
                approved = False
                reason = "Viewer is excluded by Session Shield."
            published_at = message.published_at or message.first_seen_at
            feed_items.append({
                "message_id": message.message_id,
                "session_id": message.session_id,
                "user_id": message.user_channel_id,
                "username": message.username or "Unknown viewer",
                "text": "" if content_expired else message.text,
                "content_expired": content_expired,
                "published_at": published_at,
                "time_label": published_at.strftime("%H:%M") if published_at else "Time unknown",
                "is_super_chat": bool(message.is_super_chat),
                "super_chat_amount": super_chat_amount_label(message) if message.is_super_chat else None,
                "is_shielded": is_shielded,
                "approved": bool(approved),
                "reason": reason,
                "category": moderation_result.get("category", "PENDING"),
                "moderation_state": moderation_task.state if moderation_task else "MISSING",
                "reply_state": reply_task.state if reply_task else "MISSING",
                "reply_result": reply_result,
                "reply_task_id": reply_task.id if reply_task else None,
                "priority": reply_result.get("priority", "NORMAL"),
                "ease": reply_result.get("ease_to_answer", "EASY"),
                "ai_hint": reply_result.get("ai_hint"),
                "answer": reply_result.get("suggested_live_response"),
                "answer_validation": reply_result.get("answer_validation", "NOT_CHECKED"),
                "hint_validation": reply_result.get("hint_validation", "NOT_CHECKED"),
                "needs_research": bool(reply_result.get("needs_research", False)),
                "selection_reason": reply_result.get("selection_reason", ""),
                "question_type": reply_result.get("question_type", "UNCLEAR"),
            })

        shown_items = [item for item in feed_items if item["approved"]]
        hidden_items = [item for item in feed_items if not item["approved"]]
        queue_items = [
            item for item in shown_items
            if item["reply_state"] == TaskState.COMPLETED.value
            and item["reply_result"].get("selected") is True
        ]
        queue_items.sort(key=lambda item: (
            not item["is_super_chat"],
            {"HIGH": 0, "NORMAL": 1, "LOW": 2}.get(item["priority"], 1),
            str(item["published_at"] or ""),
        ))
        super_chats = [item for item in shown_items if item["is_super_chat"]]
        shield_rows = session.execute(
            select(SessionShieldRecord, LiveSessionRecord)
            .join(LiveSessionRecord, LiveSessionRecord.session_id == SessionShieldRecord.session_id)
            .where(
                SessionShieldRecord.active.is_(True),
                LiveSessionRecord.status == "ACTIVE",
            )
            .order_by(SessionShieldRecord.created_at.desc())
        ).all()
        shields = [{
            "id": shield.id,
            "username": shield.display_name or "Unknown viewer",
            "user_id": shield.user_channel_id,
            "session_id": shield.session_id,
            "category": (shield.reason or "Session Shield").split(";")[0].replace("Session Shield suggestion: ", ""),
            "confidence": shield.confidence,
            "created_at": shield.created_at,
        } for shield, _live_session in shield_rows]

        profile_count = session.scalar(select(func.count()).select_from(CommunityViewerProfile)) or 0
        returning_count = session.scalar(
            select(func.count()).select_from(CommunityViewerProfile).where(
                CommunityViewerProfile.distinct_sessions_seen >= 2
            )
        ) or 0
        returning_member_count = session.scalar(
            select(func.count()).select_from(CommunityViewerProfile).where(
                CommunityViewerProfile.distinct_sessions_seen >= 2,
                CommunityViewerProfile.is_member.is_(True),
            )
        ) or 0

        lead = queue_items[0] if queue_items else (shown_items[0] if shown_items else None)
        lead_profile = None
        lead_memories = []
        if lead and lead["user_id"]:
            lead_profile_row = session.get(CommunityViewerProfile, lead["user_id"])
            all_saved_memories = list(session.scalars(
                select(CommunityMemory).where(
                    CommunityMemory.user_channel_id == lead["user_id"],
                    CommunityMemory.status == "SAVED",
                ).order_by(CommunityMemory.created_at.desc()).limit(2)
            ))
            lead_memories = [
                {
                    "id": memory.id,
                    "summary": memory.summary,
                    "follow_up": memory.suggested_follow_up,
                    "type": memory.memory_type.title(),
                    "created_at": memory.reviewed_at or memory.created_at,
                    "unused": memory.last_used_session_id != lead["session_id"],
                }
                for memory in all_saved_memories
            ]
            if lead_profile_row:
                lead_profile = {
                    "display_name": lead_profile_row.display_name or lead["username"],
                    "handle": lead_profile_row.user_channel_id,
                    "is_member": lead_profile_row.is_member,
                    "distinct_sessions_seen": lead_profile_row.distinct_sessions_seen,
                    "message_count": lead_profile_row.message_count,
                    "first_seen_at": lead_profile_row.first_seen_at,
                }

        return {
            "total_messages": total_messages,
            "total_tasks": total_tasks,
            "pending_tasks": pending_tasks,
            "total_batches": total_batches,
            "active": bool(active_sessions),
            "session_label": "active stream" if active_sessions else "most recent stream",
            "current_session_count": len(messages),
            "shown_items": shown_items,
            "hidden_items": hidden_items,
            "queue_items": queue_items,
            "super_chats": super_chats,
            "shields": shields,
            "profile_count": profile_count,
            "returning_count": returning_count,
            "returning_member_count": returning_member_count,
            "lead": lead,
            "lead_profile": lead_profile,
            "lead_memories": lead_memories,
        }


def _navigate_to(destination):
    st.session_state["workspace_nav"] = destination


st.markdown(
    """
    <style>
      .block-container {padding-top: .65rem; padding-bottom: 1rem; max-width: 1900px;}
      [data-testid="stSidebar"] {background: #f5f7fb; border-right: 1px solid #e1e7f0;}
      [data-testid="stMetric"] {background: #fff; border: 1px solid #e2e8f0; padding: .55rem .7rem; border-radius: 12px;}
      [data-testid="stMetricLabel"] {font-size: .75rem; color: #52627b;}
      [data-testid="stMetricValue"] {font-size: 1.35rem; color: #14213b;}
      [data-testid="stVerticalBlockBorderWrapper"] {border-color: #e1e7f0; border-radius: 14px; background: #fff;}
      div.stButton > button {border-radius: 9px; font-weight: 600; min-height: 2.15rem;}
      [data-testid="stAppViewContainer"] {background: #f7f9fc;}
      .tt-brand {padding: .25rem .35rem .5rem 0; color: #14213b; line-height: 1.2;}
      .tt-brand strong {font-size: 1.45rem; letter-spacing: -.04em;}
      .tt-brand span {display: block; font-size: .82rem; color: #64748b; margin-top: .18rem;}
      .tt-muted {color: #687991; font-size: .83rem;}
      .tt-tag {display: inline-block; border-radius: 999px; padding: .15rem .48rem; font-size: .72rem; font-weight: 650; margin-right: .2rem;}
      .tt-blue {background: #e4efff; color: #1656bd;}
      .tt-green {background: #dff7ea; color: #11683d;}
      .tt-purple {background: #eee8ff; color: #6039bf;}
      .tt-red {background: #ffe4e4; color: #aa2424;}
      .tt-amber {background: #fff1cb; color: #905200;}
      .tt-chat-copy {font-size: .98rem; line-height: 1.45; color: #17253e;}
      div[data-testid="stHorizontalBlock"] {gap: .8rem;}
      @media (max-width: 900px) {
        .block-container {padding-left: .7rem; padding-right: .7rem;}
        [data-testid="stMetricValue"] {font-size: 1rem;}
      }
    </style>
    """,
    unsafe_allow_html=True,
)

dashboard = load_dashboard_snapshot()
header = st.columns([1.65, 0.78, 0.82, 0.82, 0.82, 0.78, 0.94])
with header[0]:
    st.markdown(
        '<div class="tt-brand"><strong>TradeThesis</strong><span>YouTube Live Dashboard</span></div>',
        unsafe_allow_html=True,
    )
header[1].metric("Stream", "LIVE" if dashboard["active"] else "Offline")
header[2].metric("Chat messages", dashboard["current_session_count"])
header[3].metric("Approved / shown", len(dashboard["shown_items"]))
header[4].metric("Filtered / hidden", len(dashboard["hidden_items"]))
header[5].metric("Super Chats", sum(1 for item in dashboard["shown_items"] if item["is_super_chat"]))
header[6].metric("Returning members", dashboard["returning_member_count"])

with st.sidebar:
    st.markdown("## TradeThesis")
    st.caption("Live stream control room")
    nav_items = [
        "Dashboard", "Live Chat", "Reply Queue", "Super Chats", "Members",
        "Community Memory", "Filtered Messages", "Shield / Blocks", "Analytics",
        "Live Polls", "Settings", "Past Videos Cleanup",
    ]
    page = st.radio(
        "Workspace",
        nav_items,
        index=0,
        key="workspace_nav",
        format_func=lambda value: {
            "Dashboard": "▦  Dashboard",
            "Live Chat": "▣  Live Chat",
            "Reply Queue": "☷  Reply Queue",
            "Super Chats": "＄  Super Chats",
            "Members": "♙  Members",
            "Community Memory": "♡  Community Memory",
            "Filtered Messages": "⊘  Filtered Messages",
            "Shield / Blocks": "⛨  Shield / Blocks",
            "Analytics": "▥  Analytics",
            "Live Polls": "▤  Live Polls",
            "Settings": "⚙  Settings",
            "Past Videos Cleanup": "⌫  Past Videos Cleanup",
        }.get(value, value),
    )
    st.divider()
    st.caption("SESSION SHIELD")
    st.success("Internal AI-input filter ready")
    st.caption("Active YouTube bans/mutes/timeouts are not enabled.")
    st.caption("Database: " + ("DATABASE_URL configured" if os.environ.get("DATABASE_URL") else "local SQLite"))

if page == "Dashboard":
    st.markdown("### Live Chat <span class='tt-tag tt-green'>AI APPROVED</span>", unsafe_allow_html=True)
    st.caption(
        "The default stream view shows only safety-cleared, useful messages. "
        "Filtered message bodies stay hidden; Super Chats retain priority only after safety clearance."
    )
    chat_col, detail_col = st.columns([2.2, 1.0], gap="medium")
    with chat_col:
        st.markdown("#### Live conversation")
        st.caption(f"Recent approved messages · {dashboard['session_label']} · Super Chats first")
        if not dashboard["shown_items"]:
            st.info("No messages have passed the AI safety and usefulness checks yet. No sample comments are fabricated here.")
        for item in dashboard["shown_items"][:6]:
            with st.container(border=True):
                message_col, response_col = st.columns([1.08, 1.0], gap="small")
                with message_col:
                    member_labels = []
                    if item["user_id"]:
                        with db_session() as session:
                            profile_row = session.get(CommunityViewerProfile, item["user_id"])
                            if profile_row and profile_row.is_member is True:
                                member_labels.append("MEMBER")
                            if profile_row and profile_row.distinct_sessions_seen >= 2:
                                member_labels.append("RETURNING")
                    safe_username = escape(str(item["username"]))
                    safe_time_label = escape(str(item["time_label"]))
                    tag_html = ""
                    if item["is_super_chat"]:
                        safe_amount = escape(str(item["super_chat_amount"]))
                        tag_html += '<span class="tt-tag tt-red">SUPER CHAT · ' + safe_amount + '</span>'
                    if "MEMBER" in member_labels:
                        tag_html += '<span class="tt-tag tt-green">MEMBER</span>'
                    if "RETURNING" in member_labels:
                        tag_html += '<span class="tt-tag tt-purple">RETURNING</span>'
                    st.markdown(f"**{safe_username}**  <span class='tt-muted'>{safe_time_label}</span> {tag_html}", unsafe_allow_html=True)
                    st.write(item["text"])
                    st.caption(
                        f"{item['question_type'].replace('_', ' ').title()} · "
                        f"{item['priority']} priority · {item['category']}"
                    )
                with response_col:
                    if item["ease"] in {"MODERATE", "DIFFICULT"}:
                        st.markdown(f"**{item['ease'].title()} · AI hint**")
                        if item["ai_hint"]:
                            st.info(item["ai_hint"])
                        else:
                            st.warning("The AI hint needs review before use.")
                        st.markdown("**Suggested response · exactly 2 sentences**")
                        if item["answer"]:
                            st.write(item["answer"])
                            if item["answer_validation"] == "REVIEW_REQUIRED":
                                st.warning("Sentence-count check needs review; treat this as a draft.")
                        else:
                            st.warning("No two-sentence response was returned.")
                        if item["hint_validation"] == "REVIEW_REQUIRED":
                            st.caption("The hint is missing or needs review.")
                        if item["needs_research"]:
                            st.warning("Research needed before presenting current or disputed facts.")
                    else:
                        # UI deliberately provides no AI content for easy items.
                        st.caption("Easy question · no AI hint or suggested response")
                    if item["reply_task_id"] and item["reply_state"] == TaskState.COMPLETED.value and item["reply_result"].get("selected"):
                        done_col, skip_col = st.columns(2)
                        if done_col.button("Mark answered", key=f"dashboard-answered-{item['reply_task_id']}"):
                            with db_session() as session:
                                task = session.get(LiveMessageTask, item["reply_task_id"])
                                if task and task.state == TaskState.COMPLETED.value:
                                    task.state = TaskState.ANSWERED.value
                                    session.commit()
                            st.rerun()
                        if skip_col.button("Skip", key=f"dashboard-skip-{item['reply_task_id']}"):
                            with db_session() as session:
                                task = session.get(LiveMessageTask, item["reply_task_id"])
                                if task and task.state == TaskState.COMPLETED.value:
                                    task.state = TaskState.DISMISSED.value
                                    session.commit()
                            st.rerun()

    with detail_col:
        st.markdown("#### Selected message details")
        lead = dashboard["lead"]
        if lead is None:
            st.caption("A selected message and member details will appear here when an approved conversation is available.")
        else:
            with st.container(border=True):
                st.markdown(f"**{lead['username']}** · {lead['time_label']}")
                if lead["is_super_chat"]:
                    st.markdown(f"💛 **{lead['super_chat_amount']} Super Chat**")
                st.write(lead["text"])
                st.caption(f"{lead['question_type'].replace('_', ' ').title()} · {lead['ease'].title()}")
                if lead["ease"] in {"MODERATE", "DIFFICULT"}:
                    st.markdown("**AI hint**")
                    st.write(lead["ai_hint"] or "Hint needs review.")
                    st.markdown("**Suggested response (2 sentences)**")
                    st.write(lead["answer"] or "No response returned.")
            profile = dashboard["lead_profile"]
            st.markdown("#### Member info")
            if profile:
                profile_c1, profile_c2, profile_c3 = st.columns(3)
                profile_c1.metric("Visits", profile["distinct_sessions_seen"])
                profile_c2.metric("Messages", profile["message_count"])
                profile_c3.metric("Memories", len(dashboard["lead_memories"]))
                member_flag = "Member" if profile["is_member"] is True else "Membership unknown" if profile["is_member"] is None else "Not marked as member"
                st.caption(f"{profile['display_name']} · {member_flag} · First seen {profile['first_seen_at'].strftime('%d %b %Y')}")
                saved_memories = dashboard["lead_memories"]
                if saved_memories:
                    st.markdown("**Past memories**")
                    for memory in saved_memories[:2]:
                        st.caption(f"{memory['type']} · {memory['created_at'].strftime('%d %b %Y')}")
                        st.write(memory["summary"])
                    next_memory = next((memory for memory in saved_memories if memory["unused"]), None)
                    if next_memory:
                        with st.container(border=True):
                            st.markdown("✨ **Suggested follow-up**")
                            st.write(next_memory["follow_up"])
                            if st.button("Used this follow-up", key=f"dashboard-memory-used-{next_memory['id']}"):
                                with db_session() as session:
                                    saved = session.get(CommunityMemory, next_memory["id"])
                                    if saved:
                                        from youtube_bot_dashboard.storage import utc_now
                                        saved.last_used_at = utc_now()
                                        saved.last_used_session_id = lead["session_id"]
                                        session.commit()
                                st.rerun()
                else:
                    st.caption("No saved memory for this viewer yet.")
            else:
                st.caption("No stable viewer profile is available for this message.")

    st.divider()
    panel_cols = st.columns(4, gap="small")
    with panel_cols[0]:
        with st.container(border=True):
            st.markdown("**Reply Queue**")
            st.caption(f"{len(dashboard['queue_items'])} selected")
            for item in dashboard["queue_items"][:5]:
                paid_tag = f" · {item['super_chat_amount']}" if item["is_super_chat"] else ""
                st.caption(f"{item['username']}{paid_tag} · {item['time_label']}")
            if not dashboard["queue_items"]:
                st.caption("No selected responses waiting.")
            st.button("View all →", key="dash-view-reply", on_click=_navigate_to, args=("Reply Queue",), use_container_width=True)
    with panel_cols[1]:
        with st.container(border=True):
            st.markdown("**Recent Super Chats**")
            st.caption(f"{len(dashboard['super_chats'])} safety-cleared")
            for item in dashboard["super_chats"][:4]:
                st.caption(f"{item['username']} · {item['super_chat_amount']} · {item['time_label']}")
            if not dashboard["super_chats"]:
                st.caption("No safety-cleared Super Chats yet.")
            st.button("View all →", key="dash-view-super", on_click=_navigate_to, args=("Super Chats",), use_container_width=True)
    with panel_cols[2]:
        with st.container(border=True):
            st.markdown("**Filtered Messages · Hidden**")
            st.caption(f"{len(dashboard['hidden_items'])} withheld from default view")
            for item in dashboard["hidden_items"][:4]:
                # Intentionally display metadata only; never show hidden raw comment text here.
                st.caption(f"{item['username']} · {item['category']} · {item['reply_state']}")
            if not dashboard["hidden_items"]:
                st.caption("No hidden message metadata in the recent snapshot.")
            st.button("Review status →", key="dash-view-hidden", on_click=_navigate_to, args=("Filtered Messages",), use_container_width=True)
    with panel_cols[3]:
        with st.container(border=True):
            st.markdown("**Shield Status**")
            st.caption(f"{len(dashboard['shields'])} active session shields")
            for shield in dashboard["shields"][:4]:
                st.caption(f"{shield['username']} · {shield['category']}")
            if not dashboard["shields"]:
                st.caption("No active Session Shield filters.")
            st.button("Manage →", key="dash-view-shield", on_click=_navigate_to, args=("Shield / Blocks",), use_container_width=True)

elif page == "Overview":
    # Backward compatibility if an old browser session still contains the former label.
    st.session_state["workspace_nav"] = "Dashboard"
    st.rerun()

elif page == "Live Polls":
    st.subheader("Live Polls")
    st.caption(
        "Draft questions now, then launch them into YouTube live chat when a broadcast is active. "
        "YouTube allows one active poll per chat; ending a poll requests the final result tallies."
    )
    st.info(
        "Saving a draft or previewing it does not publish anything. Launch and End poll are explicit YouTube actions. "
        "The connected OAuth account needs the live-chat poll scope."
    )

    with st.form("create_live_poll_draft"):
        question = st.text_input(
            "Poll question",
            max_chars=160,
            placeholder="What should we break down next?",
        )
        st.caption("Add 2–4 different options. Leave unused option fields blank.")
        opt_a, opt_b = st.columns(2)
        option_1 = opt_a.text_input("Option 1", max_chars=80, placeholder="Support and resistance")
        option_2 = opt_b.text_input("Option 2", max_chars=80, placeholder="VWAP")
        opt_c, opt_d = st.columns(2)
        option_3 = opt_c.text_input("Option 3 (optional)", max_chars=80, placeholder="Risk management")
        option_4 = opt_d.text_input("Option 4 (optional)", max_chars=80, placeholder="Trade review")
        save_draft = st.form_submit_button("Save poll draft", type="primary", use_container_width=True)

    if save_draft:
        try:
            with db_session() as session:
                draft = create_poll_draft(
                    session,
                    question_text=question,
                    options=[option_1, option_2, option_3, option_4],
                )
                draft_id = draft.id
                session.commit()
            st.success(f"Poll draft #{draft_id} saved. It has not been sent to YouTube.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    st.divider()
    st.subheader("Manage polls")
    with db_session() as session:
        poll_rows = list(session.scalars(
            select(LivePollRecord).order_by(LivePollRecord.created_at.desc()).limit(50)
        ))
        active_sessions = list(session.scalars(
            select(LiveSessionRecord)
            .where(LiveSessionRecord.status == "ACTIVE", LiveSessionRecord.live_chat_id.is_not(None))
            .order_by(LiveSessionRecord.started_at.desc())
        ))

    if not poll_rows:
        st.caption("No poll drafts yet. Save a question above; you can prepare polls before the channel goes live.")
    else:
        poll_by_id = {poll.id: poll for poll in poll_rows}
        selected_poll_id = st.selectbox(
            "Choose a poll",
            options=[poll.id for poll in poll_rows],
            format_func=lambda poll_id: (
                f"#{poll_id} · {poll_by_id[poll_id].status} · "
                f"{poll_by_id[poll_id].question_text}"
            ),
        )
        poll = poll_by_id[selected_poll_id]
        with st.container(border=True):
            st.caption(f"Poll #{poll.id} · {poll.status}")
            st.markdown(f"**{poll.question_text}**")
            current_options = poll_options(poll)
            for index, option in enumerate(current_options, start=1):
                st.write(f"{index}. {option}")
            if poll.status == POLL_DRAFT:
                st.caption("Preview only · this draft is not visible in YouTube chat.")
                if active_sessions:
                    active_by_id = {live_session.session_id: live_session for live_session in active_sessions}
                    selected_session_id = st.selectbox(
                        "Active YouTube live chat",
                        options=[live_session.session_id for live_session in active_sessions],
                        format_func=lambda session_id: (
                            f"{active_by_id[session_id].broadcast_id or 'Broadcast'} · "
                            f"{active_by_id[session_id].live_chat_id} · "
                            f"{active_by_id[session_id].source}"
                        ),
                        key=f"poll-session-{poll.id}",
                    )
                    if st.button(
                        "Launch poll on YouTube",
                        type="primary",
                        use_container_width=True,
                        key=f"launch-poll-{poll.id}",
                    ):
                        try:
                            outcome = launch_poll_on_youtube(
                                SessionFactory,
                                poll_id=poll.id,
                                session_id=selected_session_id,
                            )
                            st.success("Poll launched in YouTube live chat.")
                            st.caption(f"YouTube poll message ID: {outcome['youtube_message_id']}")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Poll was not confirmed as launched: {exc}")
                else:
                    st.warning(
                        "No active live chat is registered yet. You can keep drafting polls now; "
                        "the launch control appears when the listener/source registers an active chat."
                    )
            elif poll.status == POLL_ACTIVE:
                st.warning("This poll is live in YouTube chat. Ending it is an external action.")
                if st.button(
                    "End poll & collect results",
                    type="primary",
                    use_container_width=True,
                    key=f"end-poll-{poll.id}",
                ):
                    try:
                        outcome = end_poll_on_youtube(SessionFactory, poll_id=poll.id)
                        st.success("YouTube poll closed and the returned results were saved.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Poll could not be confirmed as closed: {exc}")
            elif poll.status == POLL_CLOSED:
                st.success("Poll closed · results saved")
                results = {}
                try:
                    results = json.loads(poll.results_json or "{}")
                except json.JSONDecodeError:
                    results = {}
                result_options = results.get("options", [])
                if result_options:
                    st.dataframe(
                        [
                            {
                                "Option": option.get("option_text", ""),
                                "Votes": option.get("tally") if option.get("tally") is not None else "Not returned",
                            }
                            for option in result_options
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )
                    numeric_values = []
                    for option in result_options:
                        try:
                            numeric_values.append(int(option.get("tally")))
                        except (TypeError, ValueError):
                            numeric_values.append(None)
                    if numeric_values and all(value is not None for value in numeric_values):
                        st.bar_chart(
                            {
                                "Option": [option.get("option_text", "") for option in result_options],
                                "Votes": numeric_values,
                            },
                            x="Option",
                            y="Votes",
                        )
                    if all(option.get("tally") is None for option in result_options):
                        st.caption("YouTube did not include vote tallies in the transition response.")
                else:
                    st.caption("The poll was closed, but no option tallies were included in the saved response.")
                if poll.closed_at:
                    st.caption(f"Closed at: {poll.closed_at}")
            if poll.last_error:
                st.error("Last action error: " + poll.last_error)

elif page == "Settings":
    st.subheader("Live AI queue settings")
    st.write(
        "These settings are persisted in the database. The default live batch window is 2 seconds. "
        "The worker will flush a batch when the time window expires, the message cap is reached, "
        "or the estimated input-token budget would be exceeded."
    )
    with db_session() as session:
        config = get_live_queue_config(session)

    with st.form("live_queue_settings"):
        batch_delay = st.number_input(
            "Batch delay (seconds)",
            min_value=0.25,
            max_value=30.0,
            value=float(config.batch_delay_seconds),
            step=0.25,
            help="Default: 2 seconds. A partial batch is flushed when its oldest message reaches this age.",
        )
        request_gap = st.number_input(
            "Minimum gap between AI request starts (seconds)",
            min_value=0.1,
            max_value=30.0,
            value=float(config.minimum_request_interval_seconds),
            step=0.1,
            help="Default: 1 second. This is separate from the 2-second collection window and is enforced by the single background worker.",
        )
        max_messages = st.number_input(
            "Maximum messages per batch",
            min_value=1,
            max_value=500,
            value=int(config.max_messages_per_batch),
            step=1,
        )
        max_tokens = st.number_input(
            "Approximate total input-token budget",
            min_value=1000,
            max_value=100000,
            value=int(config.max_input_tokens),
            step=1000,
            help="Includes a reserved allowance for the system prompt. Provider-specific token counting will replace estimates where available.",
        )
        prompt_reserve = st.number_input(
            "Reserved tokens for system prompt",
            min_value=0,
            max_value=int(max_tokens) - 101,
            value=min(int(config.system_prompt_reserve_tokens), int(max_tokens) - 101),
            step=250,
            help="Keeps room for the long instructions and output-format contract.",
        )
        shield_threshold = st.number_input(
            "Session Shield confidence threshold",
            min_value=0.50,
            max_value=1.00,
            value=float(config.session_shield_confidence_threshold),
            step=0.05,
            help="A moderation suggestion must pass this threshold and match a violation category before the backend internally excludes the user from Reply Finder. This never mutes or bans them on YouTube.",
        )
        providers = st.multiselect(
            "Providers available for testing",
            options=[ProviderName.GEMINI, ProviderName.QWEN, ProviderName.DEEPSEEK, ProviderName.KIMI],
            default=list(config.enabled_providers),
            format_func=lambda provider: provider.value.title(),
        )
        strategy = st.selectbox(
            "Provider selection strategy",
            options=[
                ProviderStrategy.ROUND_ROBIN,
                ProviderStrategy.GEMINI_ONLY,
                ProviderStrategy.QWEN_ONLY,
                ProviderStrategy.DEEPSEEK_ONLY,
                ProviderStrategy.KIMI_ONLY,
            ],
            index=[
                ProviderStrategy.ROUND_ROBIN,
                ProviderStrategy.GEMINI_ONLY,
                ProviderStrategy.QWEN_ONLY,
                ProviderStrategy.DEEPSEEK_ONLY,
                ProviderStrategy.KIMI_ONLY,
            ].index(config.provider_strategy),
            format_func=lambda option: {
                ProviderStrategy.ROUND_ROBIN: "Round robin across enabled providers",
                ProviderStrategy.GEMINI_ONLY: "Gemini only",
                ProviderStrategy.QWEN_ONLY: "Qwen only",
                ProviderStrategy.DEEPSEEK_ONLY: "DeepSeek only",
                ProviderStrategy.KIMI_ONLY: "Kimi only",
            }[option],
            help="Round robin advances independently for Moderation and Reply Finder. Fixed-provider modes are useful for controlled comparisons; retries stay with the originally assigned provider.",
        )
        max_attempts = st.number_input(
            "Maximum provider attempts per batch item",
            min_value=1,
            max_value=10,
            value=int(config.max_attempts),
            step=1,
        )
        save = st.form_submit_button("Save live queue settings", type="primary")

    if save:
        try:
            new_config = LiveQueueConfig(
                batch_delay_seconds=float(batch_delay),
                minimum_request_interval_seconds=float(request_gap),
                max_messages_per_batch=int(max_messages),
                max_input_tokens=int(max_tokens),
                system_prompt_reserve_tokens=int(prompt_reserve),
                session_shield_confidence_threshold=float(shield_threshold),
                provider_strategy=strategy,
                enabled_providers=tuple(providers),
                max_attempts=int(max_attempts),
                retry_base_seconds=config.retry_base_seconds,
            )
            with db_session() as session:
                update_live_queue_config(session, new_config)
                session.commit()
            st.success("Settings saved. New batches will use the updated configuration.")
        except (ValueError, TypeError) as exc:
            st.error(f"Settings were not saved: {exc}")

    st.divider()
    st.subheader("Live-message text retention")
    st.caption(
        "This controls raw chat-message content only. It does not delete AI task results, "
        "moderation decisions, member profiles, approved memories, or historical-video comments."
    )
    with db_session() as session:
        saved_retention_days = get_live_message_retention_days(session)
    with st.form("live_message_retention_settings"):
        retention_days = st.slider(
            "Keep live-message text for (days)",
            min_value=0,
            max_value=365,
            value=saved_retention_days,
            step=1,
            help="0 keeps message text indefinitely. A value from 1 to 365 removes message bodies older than that many days.",
        )
        if retention_days == 0:
            st.caption("Current choice: keep message text indefinitely.")
        else:
            st.caption(
                f"Current choice: retain raw message text for {retention_days} day(s). "
                "Older content will be redacted automatically by the worker, and old batch payload copies will be scrubbed."
            )
        save_retention = st.form_submit_button("Save retention policy", type="primary")
    if save_retention:
        try:
            with db_session() as session:
                save_live_message_retention_days(session, retention_days)
                session.commit()
            st.success("Live-message retention policy saved.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    st.caption(
        "The worker checks retention on startup and then hourly. The action below runs the saved policy immediately."
    )
    if st.button("Run message cleanup now", key="run-live-message-retention", use_container_width=True):
        try:
            summary = run_live_message_retention_cleanup(SessionFactory)
            st.success(
                f"Cleanup finished · {summary['expired_messages']} message body/bodies redacted · "
                f"{summary['scrubbed_batch_payloads']} batch payload(s) scrubbed · "
                f"{summary['excluded_tasks']} pending task(s) excluded."
            )
        except Exception as exc:
            st.error(f"Message cleanup failed; no success is assumed: {exc}")

    st.divider()
    st.subheader("Not yet configurable here")
    st.write("AI API keys/model IDs, prompt caching, provider-specific token counting and the separate historical-video cleanup scheduler are not yet configurable here.")
    st.caption("Keep Gemini and Qwen credentials in environment variables or secret storage, never in these ordinary settings fields.")
    st.code("python -m youtube_bot_dashboard.live_worker", language="bash")
    st.caption("Run the worker as a separate background process after setting DATABASE_URL and the selected provider's credentials/model environment variables. For direct YouTube intake, configure the OAuth Desktop client, run python -m youtube_bot_dashboard.youtube_auth, then python -m youtube_bot_dashboard.youtube_listener. Real-broadcast verification is still pending.")

elif page == "Live Chat":
    st.subheader("AI-whitelisted live chat")
    st.caption(
        "Ordinary messages need high-confidence AI safety approval and a useful Reply Finder selection. "
        "Super Chats jump to the top after the same safety check clears them, even when there is no comment. "
        "This is a private display filter; it does not delete messages or restrict anyone on YouTube."
    )
    show_unfiltered = st.checkbox(
        "Show unfiltered messages (includes pending, uncertain, flagged, and low-value chat)",
        value=False,
        key="show_unfiltered_live_chat",
        help="Use only when you deliberately want to inspect raw incoming chat. It may include abusive or upsetting messages.",
    )

    with db_session() as session:
        active_session_ids = list(session.scalars(
            select(LiveSessionRecord.session_id)
            .where(LiveSessionRecord.status == "ACTIVE")
            .order_by(LiveSessionRecord.started_at.desc())
        ))
        query = select(LiveMessageRecord)
        if active_session_ids:
            query = query.where(LiveMessageRecord.session_id.in_(active_session_ids))
        else:
            # When no stream is active, show the most recent session for review.
            latest_session_id = session.scalar(
                select(LiveMessageRecord.session_id)
                .order_by(LiveMessageRecord.last_seen_at.desc())
                .limit(1)
            )
            if latest_session_id:
                query = query.where(LiveMessageRecord.session_id == latest_session_id)

        messages = list(session.scalars(
            query.order_by(
                LiveMessageRecord.is_super_chat.desc(),
                LiveMessageRecord.first_seen_at.desc(),
            ).limit(100)
        ))
        tasks_by_key = {}
        if messages:
            message_ids = [message.message_id for message in messages]
            tasks = list(session.scalars(
                select(LiveMessageTask).where(LiveMessageTask.message_id.in_(message_ids))
            ))
            tasks_by_key = {(task.message_id, task.pipeline): task for task in tasks}

        active_shield_keys = {
            (row[0], row[1])
            for row in session.execute(
                select(SessionShieldRecord.session_id, SessionShieldRecord.user_channel_id)
                .join(LiveSessionRecord, LiveSessionRecord.session_id == SessionShieldRecord.session_id)
                .where(
                    SessionShieldRecord.active.is_(True),
                    LiveSessionRecord.status == "ACTIVE",
                )
            ).all()
        }

        feed_items = []
        for message in messages:
            moderation_task = tasks_by_key.get((message.message_id, LivePipeline.MODERATION.value))
            reply_task = tasks_by_key.get((message.message_id, LivePipeline.REPLY_FINDER.value))

            def task_result(task):
                if task is None:
                    return {}
                try:
                    result = json.loads(task.result_json or "{}")
                except (TypeError, json.JSONDecodeError):
                    return {}
                return result if isinstance(result, dict) else {}

            moderation_result = task_result(moderation_task)
            reply_result = task_result(reply_task)
            is_shielded = bool(
                message.user_channel_id
                and (message.session_id, message.user_channel_id) in active_shield_keys
            )
            content_expired = message.text == LIVE_MESSAGE_TEXT_EXPIRED
            approved, reason = is_ai_whitelisted_for_display(
                moderation_state=moderation_task.state if moderation_task else None,
                moderation_result=moderation_result,
                reply_finder_state=reply_task.state if reply_task else None,
                reply_finder_result=reply_result,
                is_super_chat=message.is_super_chat,
            )
            if content_expired:
                approved = False
                reason = "Message body removed by the live-message retention policy."
            if is_shielded:
                approved = False
                reason = "Viewer is currently excluded by Session Shield."
            feed_items.append({
                "message_id": message.message_id,
                "username": message.username or "Unknown viewer",
                "text": "" if content_expired else message.text,
                "content_expired": content_expired,
                "published_at": message.published_at or message.first_seen_at,
                "is_super_chat": bool(message.is_super_chat),
                "super_chat_amount": super_chat_amount_label(message) if message.is_super_chat else None,
                "is_shielded": is_shielded,
                "approved": approved,
                "reason": reason,
                "moderation_state": moderation_task.state if moderation_task else "MISSING",
                "reply_state": reply_task.state if reply_task else "MISSING",
                "category": moderation_result.get("category", "PENDING"),
            })

    if show_unfiltered:
        st.warning(
            "Unfiltered view is on. Raw messages may include harmful, upsetting, uncertain, or unproductive content. "
            "This only changes your private dashboard view; it does not change YouTube chat."
        )
        displayed_items = feed_items
    else:
        displayed_items = [item for item in feed_items if item["approved"]]

    session_label = "active stream" if active_session_ids else "most recent session"
    st.caption(
        f"Showing {len(displayed_items)} message(s) from the {session_label} "
        f"({len(feed_items)} recently ingested message(s) checked)."
    )
    if not feed_items:
        st.info("No messages have been ingested for this session yet.")
    elif not displayed_items:
        if show_unfiltered:
            st.info("No messages were found in the current feed.")
        else:
            st.info(
                "No messages have passed both AI checks yet. Incoming messages remain hidden while checks are pending, "
                "uncertain, flagged, or not selected as useful/constructive."
            )
    else:
        for item in displayed_items:
            message_time = item["published_at"].strftime("%H:%M:%S") if item["published_at"] else "Time unknown"
            with st.container(border=True):
                st.markdown(f"**{item['username']}** · {message_time}")
                if item["is_super_chat"]:
                    st.markdown(f"**💛 SUPER CHAT · {item['super_chat_amount']} · PRIORITY**")
                if item.get("content_expired"):
                    st.caption("Message body removed by retention policy; metadata and AI decisions are retained.")
                elif item["text"]:
                    st.write(item["text"])
                if show_unfiltered:
                    if item["approved"]:
                        st.caption("AI-whitelisted · safety cleared and selected as useful/constructive")
                    else:
                        st.caption(f"Hidden from default view: {item['reason']}")
    with st.expander("Technical processing status (message text is not shown here)"):
        with db_session() as session:
            rows = list(session.scalars(
                select(LiveMessageTask).order_by(LiveMessageTask.queued_at.desc()).limit(100)
            ))
        if rows:
            st.dataframe([
                {
                    "Message ID": task.message_id,
                    "Pipeline": task.pipeline,
                    "State": task.state,
                    "Provider": task.provider or "Not assigned",
                    "Attempts": task.attempt_count,
                    "Queued at": task.queued_at,
                    "Last error": task.last_error,
                }
                for task in rows
            ], use_container_width=True)
        else:
            st.caption("No task records yet.")

    st.divider()
    st.subheader("Session Shield · all-AI input filter")
    st.caption("A shielded viewer can still post on YouTube, but their subsequent messages are excluded from both AI pipelines for the rest of this session. This internal filter does not ban or mute anyone on YouTube.")
    with db_session() as session:
        shields = session.execute(
            select(SessionShieldRecord, LiveSessionRecord)
            .join(LiveSessionRecord, LiveSessionRecord.session_id == SessionShieldRecord.session_id)
            .where(SessionShieldRecord.active.is_(True), LiveSessionRecord.status == "ACTIVE")
            .order_by(SessionShieldRecord.created_at.desc())
        ).all()
        shield_items = [
            {
                "id": shield.id,
                "username": shield.display_name or "Unknown viewer",
                "user_id": shield.user_channel_id,
                "session_id": shield.session_id,
                "reason": shield.reason,
                "confidence": shield.confidence,
                "source": shield.source,
                "created_at": shield.created_at,
            }
            for shield, _live_session in shields
        ]
    if shield_items:
        st.dataframe([
            {
                "Username": item["username"],
                "User channel ID": item["user_id"],
                "Reason": item["reason"],
                "Confidence": item["confidence"],
                "Source": item["source"],
                "Added at": item["created_at"],
                "Session": item["session_id"],
            }
            for item in shield_items
        ], use_container_width=True, hide_index=True)
        option_ids = [item["id"] for item in shield_items]
        labels = {
            item["id"]: f"{item['username']} · {item['user_id']} · {item['session_id']}"
            for item in shield_items
        }
        selected_id = st.selectbox(
            "Remove an internal filter",
            options=option_ids,
            format_func=lambda value: labels[value],
        )
        if st.button("Remove Session Shield for selected user", use_container_width=True):
            selected = next(item for item in shield_items if item["id"] == selected_id)
            with db_session() as session:
                did_remove = unshield_user_for_session(
                    session,
                    session_id=selected["session_id"],
                    user_channel_id=selected["user_id"],
                )
                session.commit()
            if did_remove:
                st.success("Internal filter removed for future messages in this session. Previously excluded messages are not automatically requeued.")
                st.rerun()
    else:
        st.caption("No active Session Shield exclusions for an active session.")

elif page == "Reply Queue":
    st.subheader("Private Reply Finder queue")
    st.caption("Read the question here and answer verbally on stream. Nothing is posted to YouTube by this dashboard.")
    with db_session() as session:
        rows = session.execute(
            select(LiveMessageRecord, LiveMessageTask)
            .join(LiveMessageTask, LiveMessageTask.message_id == LiveMessageRecord.message_id)
            .where(
                LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
                LiveMessageTask.state == TaskState.COMPLETED.value,
                LiveMessageRecord.text != LIVE_MESSAGE_TEXT_EXPIRED,
            )
            .order_by(LiveMessageRecord.is_super_chat.desc(), LiveMessageTask.completed_at.asc())
            .limit(250)
        ).all()
        shown = []
        for message, task in rows:
            try:
                result = json.loads(task.result_json or "{}")
            except json.JSONDecodeError:
                result = {}
            if result.get("selected"):
                shown.append({
                    "task_id": task.id,
                    "message_id": message.message_id,
                    "session_id": message.session_id,
                    "user_id": message.user_channel_id,
                    "message": message.text,
                    "username": message.username or "Unknown viewer",
                    "is_super_chat": bool(message.is_super_chat),
                    "super_chat_amount": super_chat_amount_label(message) if message.is_super_chat else None,
                    "completed_at": task.completed_at,
                    "priority": result.get("priority", "NORMAL"),
                    "stance": result.get("trading_stance", "NOT_APPLICABLE"),
                    "ease": result.get("ease_to_answer", "MODERATE"),
                    "question_type": result.get("question_type", "UNCLEAR"),
                    "selection_reason": result.get("selection_reason", ""),
                    "answer_angle": result.get("suggested_answer_angle"),
                    "live_response": result.get("suggested_live_response"),
                    "answer_validation": result.get("answer_validation", "NOT_CHECKED"),
                    "needs_research": bool(result.get("needs_research", False)),
                    "provider": task.provider or "Unknown",
                })

    if shown:
        shown.sort(key=lambda item: (
            not item["is_super_chat"],
            item["completed_at"] is None,
            str(item["completed_at"] or ""),
        ))
        st.caption(f"{len(shown)} selected message(s) waiting for your attention · Super Chats are prioritized first")
        item = shown[0]
        with st.container(border=True):
            st.subheader(item["username"])
            if item["is_super_chat"]:
                st.markdown(f"**💛 SUPER CHAT · {item['super_chat_amount']} · PRIORITY**")
            # A returning-viewer cue is deliberately brief: glance at it, speak
            # naturally if relevant, and keep the live conversation moving.
            with db_session() as session:
                profile = session.get(CommunityViewerProfile, item["user_id"]) if item["user_id"] else None
                saved_memories = list(session.scalars(select(CommunityMemory).where(
                    CommunityMemory.user_channel_id == item["user_id"],
                    CommunityMemory.status == "SAVED",
                ).order_by(CommunityMemory.created_at.desc()).limit(2))) if item["user_id"] else []
                memory = next((
                    candidate for candidate in saved_memories
                    if candidate.last_used_session_id != item["session_id"]
                ), None)
            if profile and (profile.distinct_sessions_seen >= 2 or profile.is_member is True):
                member_label = "MEMBER · " if profile.is_member is True else ""
                if profile.distinct_sessions_seen >= 2:
                    st.caption(
                        f"{member_label}RETURNING VIEWER · Seen in {profile.distinct_sessions_seen} streams · "
                        f"First seen {profile.first_seen_at.strftime('%d %b %Y')}"
                    )
                elif profile.is_member is True:
                    st.caption(f"MEMBER · First seen {profile.first_seen_at.strftime('%d %b %Y')}")
                if profile.distinct_sessions_seen >= 2 and memory:
                    st.markdown("**Remember:** " + memory.summary)
                    st.caption("Possible follow-up: " + memory.suggested_follow_up)
                    if st.button("Used this follow-up", key=f"memory-used-{memory.id}-{item['task_id']}"):
                        with db_session() as session:
                            saved = session.get(CommunityMemory, memory.id)
                            if saved:
                                from youtube_bot_dashboard.storage import utc_now
                                saved.last_used_at = utc_now()
                                saved.last_used_session_id = item["session_id"]
                                session.commit()
                        st.rerun()
            st.caption(
                f"{item['priority']} PRIORITY · {item['ease']} QUESTION · "
                f"{item['question_type'].replace('_', ' ').title()}"
            )
            st.markdown("**Viewer asked**")
            st.write(item["message"])
            with st.expander("Why selected · technical details"):
                if item["selection_reason"]:
                    st.write(item["selection_reason"])
                st.caption(f"Trading stance: {item['stance'].replace('_', ' ').title()}")
                st.caption(f"AI provider: {item['provider']} · Message ID: {item['message_id']}")

            st.divider()
            if item["ease"] in ("MODERATE", "DIFFICULT"):
                st.markdown("**Suggested two-sentence live response**")
                if item["live_response"]:
                    st.write(item["live_response"])
                    if item["answer_validation"] == "REVIEW_REQUIRED":
                        st.warning("Sentence-count check needs review. Treat this as a draft, not a verified answer.")
                else:
                    st.warning("No suggested response was produced. Skip this question or prepare an answer before responding.")
            else:
                st.caption("Easy question · no AI hint or suggested response")

            if item["needs_research"]:
                st.warning("This question needs additional research. Do not present an unverified current or disputed fact as confirmed.")

            left, right = st.columns(2)
            if left.button("Mark answered", key=f"answered-{item['task_id']}", type="primary", use_container_width=True):
                with db_session() as session:
                    task = session.get(LiveMessageTask, item["task_id"])
                    if task and task.state == TaskState.COMPLETED.value:
                        task.state = TaskState.ANSWERED.value
                        session.commit()
                st.rerun()
            if right.button("Skip / dismiss", key=f"dismiss-{item['task_id']}", use_container_width=True):
                with db_session() as session:
                    task = session.get(LiveMessageTask, item["task_id"])
                    if task and task.state == TaskState.COMPLETED.value:
                        task.state = TaskState.DISMISSED.value
                        session.commit()
                st.rerun()
    else:
        st.caption("No selected messages are waiting. Safe Super Chats and AI-selected ordinary messages appear in Live Chat; unfiltered chat stays hidden unless explicitly enabled.")

elif page == "Community Memory":
    st.subheader("Community Memory")
    st.caption(
        "A quiet reminder for meaningful returning viewers. Ordinary chat text is not saved as a memory. "
        "Only the stable channel ID, basic visit history, and a short memory you approve are kept."
    )
    with db_session() as session:
        profile_count = session.scalar(select(func.count()).select_from(CommunityViewerProfile)) or 0
        returning_count = session.scalar(select(func.count()).select_from(CommunityViewerProfile).where(
            CommunityViewerProfile.distinct_sessions_seen >= 2
        )) or 0
        suggestion_rows = list(session.scalars(
            select(CommunityMemory).where(CommunityMemory.status == "SUGGESTED")
            .order_by(CommunityMemory.created_at.desc()).limit(20)
        ))
        saved_rows = list(session.scalars(
            select(CommunityMemory).where(CommunityMemory.status == "SAVED")
            .order_by(CommunityMemory.created_at.desc()).limit(100)
        ))
        profiles = list(session.scalars(
            select(CommunityViewerProfile).order_by(CommunityViewerProfile.last_seen_at.desc()).limit(100)
        ))

    c1, c2, c3 = st.columns(3)
    c1.metric("Viewers with profiles", profile_count)
    c2.metric("Returning viewers", returning_count)
    c3.metric("Suggested memories to review", len(suggestion_rows))
    st.caption("The live Reply Queue shows a one-glance returning-viewer note when that person’s question is selected.")

    st.divider()
    st.subheader("Quick review · save only what helps you reconnect")
    if suggestion_rows:
        profiles_by_id = {profile.user_channel_id: profile for profile in profiles}
        for candidate in suggestion_rows:
            owner = profiles_by_id.get(candidate.user_channel_id)
            display_name = (owner.display_name if owner and owner.display_name else "Unknown viewer")
            with st.container(border=True):
                st.markdown(f"**{display_name}** · {candidate.memory_type.title()}")
                st.write(candidate.summary)
                st.caption("Possible follow-up: " + candidate.suggested_follow_up)
                st.caption("Suggested by AI · not saved until you approve it")
                left, right = st.columns(2)
                if left.button("Save memory", key=f"memory-save-{candidate.id}", type="primary", use_container_width=True):
                    with db_session() as session:
                        result = review_memory_suggestion(session, memory_id=candidate.id, decision="SAVE")
                        session.commit()
                    if result == "LIMIT_REACHED":
                        st.warning("This viewer already has two saved memories. Remove one before saving another.")
                    else:
                        st.rerun()
                if right.button("Skip", key=f"memory-skip-{candidate.id}", use_container_width=True):
                    with db_session() as session:
                        review_memory_suggestion(session, memory_id=candidate.id, decision="DISMISS")
                        session.commit()
                    st.rerun()
    else:
        st.caption("No memory suggestions need review.")

    st.divider()
    st.subheader("Saved memories · maximum two per viewer")
    if saved_rows:
        profiles_by_id = {profile.user_channel_id: profile for profile in profiles}
        st.dataframe([
            {
                "Membership": ("Member" if profiles_by_id.get(memory.user_channel_id).is_member is True else "Non-member" if profiles_by_id.get(memory.user_channel_id).is_member is False else "Unknown") if profiles_by_id.get(memory.user_channel_id) else "Unknown",
                "Viewer": (profiles_by_id.get(memory.user_channel_id).display_name
                           if profiles_by_id.get(memory.user_channel_id)
                           and profiles_by_id.get(memory.user_channel_id).display_name
                           else "Unknown viewer"),
                "Memory": memory.summary,
                "Suggested follow-up": memory.suggested_follow_up,
                "Type": memory.memory_type.title(),
                "Saved": memory.reviewed_at or memory.created_at,
            }
            for memory in saved_rows
        ], use_container_width=True, hide_index=True)
        memory_ids = [memory.id for memory in saved_rows]
        memory_labels = {}
        for memory in saved_rows:
            owner = profiles_by_id.get(memory.user_channel_id)
            owner_name = owner.display_name if owner and owner.display_name else "Unknown viewer"
            memory_labels[memory.id] = f"{owner_name} · {memory.summary}"
        selected_memory = st.selectbox(
            "Forget a saved memory",
            options=memory_ids,
            format_func=lambda memory_id: memory_labels[memory_id],
        )
        if st.button("Forget selected memory"):
            with db_session() as session:
                forget_saved_memory(session, memory_id=selected_memory)
                session.commit()
            st.rerun()
    else:
        st.caption("No saved memories yet.")

    st.divider()
    st.subheader("Privacy control")
    if profiles:
        profile_options = [profile.user_channel_id for profile in profiles]
        profile_labels = {
            profile.user_channel_id: f"{profile.display_name or 'Unknown viewer'} · {profile.user_channel_id}"
            for profile in profiles
        }
        selected_viewer = st.selectbox(
            "Forget a viewer completely",
            options=profile_options,
            format_func=lambda user_id: profile_labels[user_id],
        )
        confirm_forget = st.checkbox(
            "I understand this deletes the viewer profile, visit history, and saved/suggested memories.",
            key="confirm-forget-viewer",
        )
        if st.button("Forget viewer and all memories", disabled=not confirm_forget):
            with db_session() as session:
                forget_viewer(session, user_channel_id=selected_viewer)
                session.commit()
            st.rerun()
    else:
        st.caption("No viewer profiles have been recorded yet.")

elif page == "Past Videos Cleanup":
    st.subheader("Past Videos Cleanup")
    st.write("Daily sweep default: 9:00 PM Asia/Kolkata")
    st.info(
        "The saved design calls for a rolling 20-day window per video and database-based deduplication. "
        "The YouTube OAuth connection, scheduler and comment-listing API are not connected yet."
    )
    st.write("Deletion mode: Preview and owner approval first.")
    st.write("Auto-delete: OFF.")


elif page == "Super Chats":
    st.subheader("Super Chats")
    st.caption("Paid messages are prioritized for attention only after the same high-confidence safety clearance as other visible messages.")
    safe_paid = dashboard["super_chats"]
    if not safe_paid:
        st.info("No safety-cleared Super Chats are available in the current stream snapshot.")
    for item in safe_paid:
        with st.container(border=True):
            st.markdown(f"**{item['username']}** · **{item['super_chat_amount']}** · {item['time_label']}")
            st.write(item["text"])
            if item["ease"] in {"MODERATE", "DIFFICULT"}:
                st.markdown("**AI hint**")
                st.write(item["ai_hint"] or "Hint needs review.")
                st.markdown("**Suggested response · 2 sentences**")
                st.write(item["answer"] or "No response returned.")
            elif item["reply_result"].get("selected") is True:
                st.caption("Easy question · no AI hint or suggested response.")

elif page == "Members":
    st.subheader("Members & returning viewers")
    st.caption("Profiles are matched only by stable source channel ID. Membership appears only when the source explicitly supplies it.")
    with db_session() as session:
        profiles = list(session.scalars(
            select(CommunityViewerProfile).order_by(CommunityViewerProfile.last_seen_at.desc()).limit(250)
        ))
        profile_ids = [profile.user_channel_id for profile in profiles]
        saved_memories = list(session.scalars(
            select(CommunityMemory).where(
                CommunityMemory.user_channel_id.in_(profile_ids),
                CommunityMemory.status == "SAVED",
            ).order_by(CommunityMemory.created_at.desc())
        )) if profile_ids else []
    memory_counts = {}
    for memory in saved_memories:
        memory_counts[memory.user_channel_id] = memory_counts.get(memory.user_channel_id, 0) + 1
    st.metric("Viewer profiles", dashboard["profile_count"])
    st.metric("Returning viewers", dashboard["returning_count"])
    if profiles:
        st.dataframe([{
            "Viewer": profile.display_name or "Unknown viewer",
            "Channel ID": profile.user_channel_id,
            "Membership": "Member" if profile.is_member is True else "Not marked member" if profile.is_member is False else "Unknown",
            "Streams seen": profile.distinct_sessions_seen,
            "Messages recorded": profile.message_count,
            "Saved memories": memory_counts.get(profile.user_channel_id, 0),
            "First seen": profile.first_seen_at,
            "Last seen": profile.last_seen_at,
        } for profile in profiles], use_container_width=True, hide_index=True)
    else:
        st.info("No stable viewer profiles have been recorded yet.")

elif page == "Filtered Messages":
    st.subheader("Filtered messages · hidden by default")
    st.caption("This view explains why messages are withheld without exposing their raw text. Use Live Chat's explicit unfiltered control only when you deliberately need manual review.")
    st.metric("Withheld in recent snapshot", len(dashboard["hidden_items"]))
    if dashboard["hidden_items"]:
        for item in dashboard["hidden_items"][:100]:
            with st.container(border=True):
                st.markdown(f"**{item['username']}** · {item['category']}")
                st.caption(f"Moderation: {item['moderation_state']} · Reply Finder: {item['reply_state']}")
                st.caption("Reason: " + str(item["reason"]))
                if item["is_shielded"]:
                    st.caption("Session Shield active · subsequent messages excluded from both AI pipelines.")
    else:
        st.info("No filtered messages in the current stream snapshot.")

elif page == "Shield / Blocks":
    st.subheader("Shield / Blocks")
    st.caption("These are internal Session Shield exclusions for the active stream, not confirmed YouTube bans, mutes, or timeouts.")
    if dashboard["shields"]:
        shield_map = {item["id"]: item for item in dashboard["shields"]}
        for item in dashboard["shields"]:
            with st.container(border=True):
                st.markdown(f"**{item['username']}** · {item['category']}")
                st.caption(f"Stable channel ID: {item['user_id']} · Confidence: {item['confidence'] if item['confidence'] is not None else 'not supplied'}")
                st.caption(f"Session: {item['session_id']} · Added: {item['created_at']}")
        selected_shield_id = st.selectbox(
            "Choose a session shield to remove",
            options=list(shield_map),
            format_func=lambda shield_id: f"{shield_map[shield_id]['username']} · {shield_map[shield_id]['user_id']}",
        )
        if st.button("Remove Session Shield", type="primary"):
            selected = shield_map[selected_shield_id]
            with db_session() as session:
                removed = unshield_user_for_session(session, session_id=selected["session_id"], user_channel_id=selected["user_id"])
                session.commit()
            if removed:
                st.success("Internal filter removed for future messages in this session. Previously excluded messages are not requeued.")
                st.rerun()
    else:
        st.info("No active Session Shield exclusions.")

elif page == "Analytics":
    st.subheader("Analytics")
    st.caption("Counters describe stored application records, not live YouTube viewer counts. Viewer count is not available from the current dashboard data contract.")
    a, b, c, d = st.columns(4)
    a.metric("All messages stored", dashboard["total_messages"])
    b.metric("AI tasks recorded", dashboard["total_tasks"])
    c.metric("Pending / in-flight tasks", dashboard["pending_tasks"])
    d.metric("AI batches recorded", dashboard["total_batches"])
    st.markdown("#### Current stream snapshot")
    x, y, z = st.columns(3)
    x.metric("Checked messages", dashboard["current_session_count"])
    y.metric("Approved / shown", len(dashboard["shown_items"]))
    z.metric("Filtered / hidden", len(dashboard["hidden_items"]))
    st.markdown("#### AI providers")
    provider_columns = st.columns(2)
    for column, provider in zip(provider_columns, [ProviderName.GEMINI, ProviderName.QWEN]):
        if provider_registry.has(provider):
            column.success(f"{provider.value.title()} configured")
            column.caption(f"Model: {provider_registry.get(provider).model_id}")
        else:
            column.warning(f"{provider.value.title()} not configured")

st.sidebar.caption("Database: " + ("DATABASE_URL configured" if os.environ.get("DATABASE_URL") else "local SQLite development database"))
