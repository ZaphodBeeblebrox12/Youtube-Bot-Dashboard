"""Streamlit control room for live chat, poll control, and community memory.

This UI does not ban, mute, time out, or delete YouTube users/comments. Polls are
created/closed only when the owner presses the corresponding dashboard action.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

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
from youtube_bot_dashboard.providers.registry import ProviderRegistry
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


st.title("YouTube Bot Dashboard")
st.caption("Private live-stream control room · Local-first preparation build")

with st.sidebar:
    st.header("Moderation levers")
    st.success("Lever 1 · Session Shield")
    st.caption("Internal Reply Finder filter only. People remain free to chat on YouTube.")
    st.warning("Lever 2 · Active Moderation is OFF")
    st.caption("No YouTube bans, timeouts, mutes or deletions are connected in this version.")
    page = st.radio(
        "Workspace",
        ["Overview", "Live Chat", "Reply Queue", "Community Memory", "Live Polls", "Settings", "Past Videos Cleanup"],
        index=0,
    )

if page == "Overview":
    with db_session() as session:
        messages, tasks, pending, batches = read_counts(session)
        current_config = get_live_queue_config(session)

    st.subheader("System overview")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Messages stored", messages)
    c2.metric("Pipeline tasks", tasks)
    c3.metric("Pending / in-flight", pending)
    c4.metric("Batches recorded", batches)

    st.subheader("AI providers")
    provider_columns = st.columns(2)
    for column, provider in zip(provider_columns, [ProviderName.GEMINI, ProviderName.QWEN]):
        if provider_registry.has(provider):
            adapter = provider_registry.get(provider)
            column.success(f"{provider.value.title()} configured")
            column.caption(f"Model: {adapter.model_id}")
        else:
            column.warning(f"{provider.value.title()} not configured")
            column.caption("Set the provider credentials/model in environment secrets.")

    st.subheader("Live queue configuration")
    col1, col2, col3 = st.columns(3)
    col1.metric("Batch window", f"{current_config.batch_delay_seconds:g} sec")
    col2.metric("Max messages / batch", current_config.max_messages_per_batch)
    col3.metric("AI request start gap", f"{current_config.minimum_request_interval_seconds:g} sec")
    st.caption("Provider routing: " + current_config.provider_strategy.value.replace("_", " ").title())

    st.info(
        "The read-only YouTube streamList listener and OAuth setup code are available. "
        "Actual OAuth and live-broadcast delivery still need verification when your channel is ready; "
        "the authenticated JSON bridge remains available."
    )
    st.subheader("Safety status")
    st.write("Session Shield: ready in policy code; no public YouTube action.")
    st.write("Active Moderation: disabled and not wired to enforcement APIs.")
    st.write("Past Videos Cleanup: policy and incremental-storage foundations exist; YouTube connection/scheduler are not connected.")

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
            options=[ProviderName.GEMINI, ProviderName.QWEN],
            default=list(config.enabled_providers),
            format_func=lambda provider: provider.value.title(),
        )
        strategy = st.selectbox(
            "Provider selection strategy",
            options=[
                ProviderStrategy.ROUND_ROBIN,
                ProviderStrategy.GEMINI_ONLY,
                ProviderStrategy.QWEN_ONLY,
            ],
            index=[
                ProviderStrategy.ROUND_ROBIN,
                ProviderStrategy.GEMINI_ONLY,
                ProviderStrategy.QWEN_ONLY,
            ].index(config.provider_strategy),
            format_func=lambda option: {
                ProviderStrategy.ROUND_ROBIN: "Alternate batches between enabled providers",
                ProviderStrategy.GEMINI_ONLY: "Gemini only",
                ProviderStrategy.QWEN_ONLY: "Qwen only",
            }[option],
            help="Round-robin advances independently for moderation and reply-finder queues to compare each task type fairly.",
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
    st.subheader("Not yet configurable here")
    st.write("AI API keys/model IDs, prompt caching, provider-specific token counting and cleanup schedule will be added with their adapters and background worker.")
    st.caption("Keep Gemini and Qwen credentials in environment variables or secret storage, never in these ordinary settings fields.")
    st.code("python -m youtube_bot_dashboard.live_worker", language="bash")
    st.caption("Run the worker as a separate background process after setting DATABASE_URL and the selected provider's credentials/model environment variables. For direct YouTube intake, configure the OAuth Desktop client, run python -m youtube_bot_dashboard.youtube_auth, then python -m youtube_bot_dashboard.youtube_listener. Real-broadcast veelif page == "Live Chat":
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
            approved, reason = is_ai_whitelisted_for_display(
                moderation_state=moderation_task.state if moderation_task else None,
                moderation_result=moderation_result,
                reply_finder_state=reply_task.state if reply_task else None,
                reply_finder_result=reply_result,
                is_super_chat=message.is_super_chat,
            )
            if is_shielded:
                approved = False
                reason = "Viewer is currently excluded by Session Shield."
            feed_items.append({
                "message_id": message.message_id,
                "username": message.username or "Unknown viewer",
                "text": message.text,
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

 else:
            st.caption("No live chat messages have been ingested yet.")

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
            elif item["answer_angle"]:
                st.markdown("**Quick answer cue**")
                st.write(item["answer_angle"])
            else:
                st.caption("No answer cue was provided for this easy question.")

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

st.sidebar.caption("Database: " + ("DATABASE_URL configured" if os.environ.get("DATABASE_URL") else "local SQLite development database"))
