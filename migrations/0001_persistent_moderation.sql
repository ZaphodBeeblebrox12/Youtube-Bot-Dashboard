-- PostgreSQL additive schema migration: persistent moderation review, strikes, internal restrictions, audit.
-- This migration does not enable or call YouTube enforcement APIs.
CREATE TABLE IF NOT EXISTS moderation_incidents (
    id SERIAL PRIMARY KEY,
    incident_key VARCHAR(64) NOT NULL UNIQUE,
    session_id VARCHAR(120) NOT NULL REFERENCES live_sessions(session_id) ON DELETE CASCADE,
    user_channel_id VARCHAR(120),
    display_name VARCHAR(300),
    category VARCHAR(40) NOT NULL,
    severity_score INTEGER NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    explanation TEXT NOT NULL,
    evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
    source_message_id VARCHAR(160) NOT NULL,
    provider VARCHAR(24),
    model_id VARCHAR(160),
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING_REVIEW',
    qualifies_for_strike BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMPTZ,
    reviewed_by VARCHAR(120),
    review_note TEXT,
    CONSTRAINT uq_moderation_incident_key UNIQUE(incident_key),
    CONSTRAINT uq_moderation_incident_session_id UNIQUE(session_id, id)
);
CREATE INDEX IF NOT EXISTS ix_moderation_incidents_incident_key ON moderation_incidents(incident_key);
CREATE INDEX IF NOT EXISTS ix_moderation_incidents_session_id ON moderation_incidents(session_id);
CREATE INDEX IF NOT EXISTS ix_moderation_incidents_user_channel_id ON moderation_incidents(user_channel_id);
CREATE INDEX IF NOT EXISTS ix_moderation_incidents_category ON moderation_incidents(category);
CREATE INDEX IF NOT EXISTS ix_moderation_incidents_status ON moderation_incidents(status);
CREATE INDEX IF NOT EXISTS ix_moderation_incidents_source_message_id ON moderation_incidents(source_message_id);

CREATE TABLE IF NOT EXISTS moderation_session_strikes (
    id SERIAL PRIMARY KEY,
    user_channel_id VARCHAR(120) NOT NULL,
    session_id VARCHAR(120) NOT NULL REFERENCES live_sessions(session_id) ON DELETE CASCADE,
    incident_id INTEGER NOT NULL UNIQUE REFERENCES moderation_incidents(id),
    status VARCHAR(20) NOT NULL DEFAULT 'CONFIRMED',
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    revocation_note TEXT,
    CONSTRAINT uq_moderation_strike_user_session UNIQUE(user_channel_id, session_id),
    CONSTRAINT uq_moderation_strike_incident UNIQUE(incident_id)
);
CREATE INDEX IF NOT EXISTS ix_moderation_session_strikes_user_channel_id ON moderation_session_strikes(user_channel_id);
CREATE INDEX IF NOT EXISTS ix_moderation_session_strikes_session_id ON moderation_session_strikes(session_id);
CREATE INDEX IF NOT EXISTS ix_moderation_session_strikes_incident_id ON moderation_session_strikes(incident_id);
CREATE INDEX IF NOT EXISTS ix_moderation_session_strikes_status ON moderation_session_strikes(status);

CREATE TABLE IF NOT EXISTS persistent_internal_restrictions (
    id SERIAL PRIMARY KEY,
    user_channel_id VARCHAR(120) NOT NULL,
    display_name VARCHAR(300),
    restriction_type VARCHAR(20) NOT NULL DEFAULT 'PERMANENT',
    reason TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    revoked_by VARCHAR(120),
    revocation_reason TEXT,
    related_incident_id INTEGER REFERENCES moderation_incidents(id)
);
CREATE INDEX IF NOT EXISTS ix_persistent_internal_restrictions_user_channel_id ON persistent_internal_restrictions(user_channel_id);
CREATE INDEX IF NOT EXISTS ix_persistent_internal_restrictions_active ON persistent_internal_restrictions(active);
CREATE INDEX IF NOT EXISTS ix_persistent_internal_restrictions_related_incident_id ON persistent_internal_restrictions(related_incident_id);

CREATE TABLE IF NOT EXISTS moderation_audit_events (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(48) NOT NULL,
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(120) NOT NULL,
    user_channel_id VARCHAR(120),
    actor VARCHAR(120) NOT NULL DEFAULT 'system',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_moderation_audit_events_event_type ON moderation_audit_events(event_type);
CREATE INDEX IF NOT EXISTS ix_moderation_audit_events_target_type ON moderation_audit_events(target_type);
CREATE INDEX IF NOT EXISTS ix_moderation_audit_events_target_id ON moderation_audit_events(target_id);
CREATE INDEX IF NOT EXISTS ix_moderation_audit_events_user_channel_id ON moderation_audit_events(user_channel_id);

CREATE TABLE IF NOT EXISTS moderation_appeals (
    id SERIAL PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES moderation_incidents(id),
    appellant_label VARCHAR(200) NOT NULL,
    reason TEXT NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'OPEN',
    submitted_by VARCHAR(120) NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_by VARCHAR(120),
    decided_at TIMESTAMPTZ,
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS ix_moderation_appeals_incident_id ON moderation_appeals(incident_id);
CREATE INDEX IF NOT EXISTS ix_moderation_appeals_status ON moderation_appeals(status);
