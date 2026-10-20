"""Legal holds (M18 §1, ADR 0019; decisions D1-D4, docs/plans/m18.md §12).

- ``legal_holds``: one hold on ONE matter or ONE client (D1). Identity, reason and placement are
  immutable; a release is final (``released_*`` written once, together, with the cooling-off end);
  a new hold is a new row. Progress of the S3 flags (D2) is reported honestly: ``objects_flagged``
  counts only versions the store confirmed; ``objects_in_scope`` is NULL until a full pass finished.
- ``legal_hold_release_requests``: the two-person release (D3): one pending request per hold,
  approved by a DIFFERENT principal (a CHECK, not only the API) before ``expires_at`` (72 h), or
  expired. Decisions are written once.
- ``legal_hold_objects``: which pinned version carries the S3 flag for which hold (written by the
  holds worker after the store confirmed the flag). Clearing records its intent first
  (``clear_requested_at``, committed before any OFF call) and ``cleared_at`` after the store
  confirmed it: a resumed clearing that finds a flag already OFF can tell its own interrupted work
  from a flag someone else removed (D2: the latter is an incident).
- ONE definition of "held", in SQL functions (invoker rights: RLS applies) used by the triggers,
  the API, retention extension and the holds worker (``edisc_custody.holds``):
  - ``matter_held(m)``: an active hold on the matter or its client;
  - ``client_held(c)``: an active hold on the client or any of its matters;
  - ``client_directly_held(c)``: an active hold on the client itself (exports belong to no matter);
  - ``evidence_held(e)``: an active hold covers any owner of the object (its job's matter, a job
    that linked an item behind it, its render's or report's job, a job that read an entry of the
    archive it is), or it is the archive of an export of a client held directly.
- Guards: a matter or client cannot CLOSE while held (EA005, whatever the path: the API's check
  gives the clean 409, the trigger catches any other); ``retention_until`` already never decreases
  (EA004, 0001/0021).
- ``tenants.hold_cooling_off_days`` (D4: 7..90, default 7): after a release nothing the hold
  uncovered may be destroyed before ``cooling_off_until``.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0047"
down_revision: str | None = "0046"
branch_labels = None
depends_on = None

MATTERS_GUARD = """
CREATE OR REPLACE FUNCTION guard_matters() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.retention_until < OLD.retention_until OR NEW.tenant_id <> OLD.tenant_id THEN
        RAISE EXCEPTION 'matters: retention_until may only be extended' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NOT NULL AND NEW.closed_at IS NOT NULL
       AND (NEW.closed_at, NEW.closed_by) IS DISTINCT FROM (OLD.closed_at, OLD.closed_by) THEN
        RAISE EXCEPTION 'matters: reopen before closing again' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NULL AND NEW.closed_at IS NOT NULL AND matter_held(NEW.id) THEN
        RAISE EXCEPTION 'matters: a legal hold covers matter %', NEW.id USING ERRCODE = 'EA005';
    END IF;
    RETURN NEW;
END
$$;
"""

CLIENTS_GUARD = """
CREATE OR REPLACE FUNCTION guard_clients() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.is_default <> OLD.is_default THEN
        RAISE EXCEPTION 'clients: tenant and default flag are immutable' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NOT NULL AND NEW.closed_at IS NOT NULL
       AND (NEW.closed_at, NEW.closed_by) IS DISTINCT FROM (OLD.closed_at, OLD.closed_by) THEN
        RAISE EXCEPTION 'clients: reopen before closing again' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NULL AND NEW.closed_at IS NOT NULL AND client_held(NEW.id) THEN
        RAISE EXCEPTION 'clients: a legal hold covers client %', NEW.id USING ERRCODE = 'EA005';
    END IF;
    RETURN NEW;
END
$$;
"""


def _upgrade(app: str) -> str:
    return f"""
ALTER TABLE tenants ADD COLUMN hold_cooling_off_days integer NOT NULL DEFAULT 7;
ALTER TABLE tenants ADD CONSTRAINT ck_tenants_hold_cooling_off_days
    CHECK (hold_cooling_off_days BETWEEN 7 AND 90);
GRANT UPDATE (hold_cooling_off_days) ON tenants TO "{app}";

CREATE TABLE legal_holds (
    id                  uuid NOT NULL,
    tenant_id           uuid NOT NULL,
    matter_id           uuid,
    client_id           uuid,
    reason              text NOT NULL,
    reference           text,
    placed_by           text NOT NULL,
    placed_by_principal uuid,
    placed_at           timestamptz NOT NULL DEFAULT now(),
    released_at         timestamptz,
    released_by         text,
    release_reason      text,
    cooling_off_until   timestamptz,
    flag_state          text NOT NULL DEFAULT 'pending',
    objects_flagged     bigint NOT NULL DEFAULT 0,
    objects_in_scope    bigint,
    objects_cleared     bigint NOT NULL DEFAULT 0,
    flag_incidents      bigint NOT NULL DEFAULT 0,
    flag_cursor         jsonb,
    last_pass_completed_at timestamptz,
    CONSTRAINT pk_legal_holds PRIMARY KEY (id),
    CONSTRAINT uq_legal_holds_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_legal_holds_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_legal_holds_tenant_id_matter_id_matters FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id),
    CONSTRAINT fk_legal_holds_tenant_id_client_id_clients FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id),
    CONSTRAINT ck_legal_holds_one_scope CHECK ((matter_id IS NULL) <> (client_id IS NULL)),
    CONSTRAINT ck_legal_holds_reason CHECK (length(reason) BETWEEN 1 AND 2000),
    CONSTRAINT ck_legal_holds_reference CHECK (reference IS NULL OR length(reference) BETWEEN 1 AND 200),
    CONSTRAINT ck_legal_holds_release CHECK (
        (released_at IS NULL) = (released_by IS NULL)
        AND (released_at IS NULL) = (release_reason IS NULL)
        AND (released_at IS NULL) = (cooling_off_until IS NULL)
        AND (cooling_off_until IS NULL OR cooling_off_until >= released_at)),
    CONSTRAINT ck_legal_holds_flag_state CHECK (flag_state IN ('pending', 'flagging', 'flagged', 'clearing', 'cleared')),
    CONSTRAINT ck_legal_holds_flag_counts CHECK (
        objects_flagged >= 0 AND objects_cleared >= 0 AND flag_incidents >= 0
        AND (objects_in_scope IS NULL OR objects_in_scope >= 0))
);
CREATE INDEX ix_legal_holds_tenant_id_matter_id ON legal_holds (tenant_id, matter_id) WHERE released_at IS NULL;
CREATE INDEX ix_legal_holds_tenant_id_client_id ON legal_holds (tenant_id, client_id) WHERE released_at IS NULL;

CREATE OR REPLACE FUNCTION guard_legal_holds() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.matter_id, NEW.client_id, NEW.reason, NEW.reference,
        NEW.placed_by, NEW.placed_by_principal, NEW.placed_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.matter_id, OLD.client_id, OLD.reason, OLD.reference,
        OLD.placed_by, OLD.placed_by_principal, OLD.placed_at) THEN
        RAISE EXCEPTION 'legal_holds: a hold''s identity is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.released_at IS NOT NULL
       AND (NEW.released_at, NEW.released_by, NEW.release_reason, NEW.cooling_off_until)
           IS DISTINCT FROM (OLD.released_at, OLD.released_by, OLD.release_reason, OLD.cooling_off_until) THEN
        RAISE EXCEPTION 'legal_holds: a release is final' USING ERRCODE = 'EA002';
    END IF;
    IF NEW.objects_flagged < OLD.objects_flagged OR NEW.objects_cleared < OLD.objects_cleared
       OR NEW.flag_incidents < OLD.flag_incidents THEN
        RAISE EXCEPTION 'legal_holds: progress never goes back' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE legal_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_holds FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON legal_holds USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_legal_holds_guard BEFORE UPDATE ON legal_holds FOR EACH ROW EXECUTE FUNCTION guard_legal_holds();
CREATE TRIGGER trg_legal_holds_no_delete BEFORE DELETE ON legal_holds FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON legal_holds TO "{app}";

CREATE TABLE legal_hold_release_requests (
    id                      uuid NOT NULL,
    tenant_id               uuid NOT NULL,
    hold_id                 uuid NOT NULL,
    requested_by            text NOT NULL,
    requested_by_principal  uuid NOT NULL,
    reason                  text NOT NULL,
    requested_at            timestamptz NOT NULL DEFAULT now(),
    expires_at              timestamptz NOT NULL,
    decision                text,
    decided_at              timestamptz,
    decided_by              text,
    decided_by_principal    uuid,
    CONSTRAINT pk_legal_hold_release_requests PRIMARY KEY (id),
    CONSTRAINT fk_legal_hold_release_requests_tenant_id_hold_id_legal_holds FOREIGN KEY (tenant_id, hold_id) REFERENCES legal_holds (tenant_id, id),
    CONSTRAINT ck_legal_hold_release_requests_reason CHECK (length(reason) BETWEEN 1 AND 2000),
    CONSTRAINT ck_legal_hold_release_requests_expiry CHECK (expires_at > requested_at),
    CONSTRAINT ck_legal_hold_release_requests_decision CHECK (
        (decision IS NULL) = (decided_at IS NULL)
        AND (decision IS NULL OR decision IN ('approved', 'expired'))
        AND ((decision = 'approved') = (decided_by_principal IS NOT NULL))
        AND ((decision = 'approved') = (decided_by IS NOT NULL))),
    -- the two-person rule (D3): the approver is never the requester, whatever path writes it
    CONSTRAINT ck_legal_hold_release_requests_two_people CHECK (
        decided_by_principal IS NULL OR decided_by_principal <> requested_by_principal)
);
CREATE UNIQUE INDEX ux_legal_hold_release_requests_pending ON legal_hold_release_requests (hold_id) WHERE decision IS NULL;
CREATE INDEX ix_legal_hold_release_requests_tenant_id_hold_id ON legal_hold_release_requests (tenant_id, hold_id);

CREATE OR REPLACE FUNCTION guard_legal_hold_release_requests() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.hold_id, NEW.requested_by, NEW.requested_by_principal, NEW.reason,
        NEW.requested_at, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.hold_id, OLD.requested_by, OLD.requested_by_principal, OLD.reason,
        OLD.requested_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'legal_hold_release_requests: a request is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.decision IS NOT NULL THEN
        RAISE EXCEPTION 'legal_hold_release_requests: a decision is final' USING ERRCODE = 'EA002';
    END IF;
    IF NEW.decision = 'approved' AND NEW.decided_at > OLD.expires_at THEN
        RAISE EXCEPTION 'legal_hold_release_requests: the request expired' USING ERRCODE = 'EA002';
    END IF;
    IF NEW.decision = 'expired' AND NEW.decided_at < OLD.expires_at THEN
        RAISE EXCEPTION 'legal_hold_release_requests: not expired yet' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE legal_hold_release_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_hold_release_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON legal_hold_release_requests USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_legal_hold_release_requests_guard BEFORE UPDATE ON legal_hold_release_requests FOR EACH ROW EXECUTE FUNCTION guard_legal_hold_release_requests();
CREATE TRIGGER trg_legal_hold_release_requests_no_delete BEFORE DELETE ON legal_hold_release_requests FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON legal_hold_release_requests TO "{app}";

CREATE TABLE legal_hold_objects (
    tenant_id           uuid NOT NULL,
    hold_id             uuid NOT NULL,
    evidence_object_id  uuid NOT NULL,
    storage_key         text COLLATE "C" NOT NULL,
    version_id          text NOT NULL,
    flagged_at          timestamptz NOT NULL DEFAULT now(),
    clear_requested_at  timestamptz,
    cleared_at          timestamptz,
    CONSTRAINT pk_legal_hold_objects PRIMARY KEY (hold_id, evidence_object_id),
    CONSTRAINT ck_legal_hold_objects_clearing CHECK (
        clear_requested_at IS NULL OR cleared_at IS NULL OR cleared_at >= clear_requested_at),
    CONSTRAINT fk_legal_hold_objects_tenant_id_hold_id_legal_holds FOREIGN KEY (tenant_id, hold_id) REFERENCES legal_holds (tenant_id, id),
    CONSTRAINT fk_legal_hold_objects_tenant_id_evidence_object_id_evidence_objects FOREIGN KEY (tenant_id, evidence_object_id) REFERENCES evidence_objects (tenant_id, id)
);
CREATE INDEX ix_legal_hold_objects_evidence_object_id ON legal_hold_objects (evidence_object_id);
CREATE OR REPLACE FUNCTION guard_legal_hold_objects() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.tenant_id, NEW.hold_id, NEW.evidence_object_id, NEW.storage_key, NEW.version_id, NEW.flagged_at)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.hold_id, OLD.evidence_object_id, OLD.storage_key, OLD.version_id, OLD.flagged_at)
       OR OLD.cleared_at IS NOT NULL
       OR (OLD.clear_requested_at IS NOT NULL AND NEW.clear_requested_at IS DISTINCT FROM OLD.clear_requested_at) THEN
        RAISE EXCEPTION 'legal_hold_objects: only clear_requested_at and cleared_at may be written, once each'
            USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE legal_hold_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_hold_objects FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON legal_hold_objects USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_legal_hold_objects_guard BEFORE UPDATE ON legal_hold_objects FOR EACH ROW EXECUTE FUNCTION guard_legal_hold_objects();
CREATE TRIGGER trg_legal_hold_objects_no_delete BEFORE DELETE ON legal_hold_objects FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON legal_hold_objects TO "{app}";

-- a sealed job's links never change: once a pass read them after the seal, later passes skip them
CREATE TABLE legal_hold_jobs (
    tenant_id   uuid NOT NULL,
    hold_id     uuid NOT NULL,
    job_id      uuid NOT NULL,
    links_read_sealed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_legal_hold_jobs PRIMARY KEY (hold_id, job_id),
    CONSTRAINT fk_legal_hold_jobs_tenant_id_hold_id_legal_holds FOREIGN KEY (tenant_id, hold_id) REFERENCES legal_holds (tenant_id, id),
    CONSTRAINT fk_legal_hold_jobs_tenant_id_job_id_collection_jobs FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id)
);
ALTER TABLE legal_hold_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_hold_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON legal_hold_jobs USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_legal_hold_jobs_no_update BEFORE UPDATE ON legal_hold_jobs FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_legal_hold_jobs_no_delete BEFORE DELETE ON legal_hold_jobs FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON legal_hold_jobs TO "{app}";

-- bounded reads for the flag passes and "which matters own this object" (CLAUDE.md: bounded by
-- construction): a job's objects in id order, the items behind an object, an archive's entries
CREATE INDEX ix_evidence_objects_job_id_id ON evidence_objects (job_id, id);
CREATE INDEX ix_evidence_objects_archive_evidence_id ON evidence_objects (archive_evidence_id) WHERE archive_evidence_id IS NOT NULL;
CREATE INDEX ix_items_evidence_object_id ON items (evidence_object_id);

-- ONE definition of "held" (invoker rights: RLS applies; STABLE: one snapshot per statement)
CREATE FUNCTION matter_held(m uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM edisc.legal_holds h
        WHERE h.released_at IS NULL
          AND (h.matter_id = m OR h.client_id = (SELECT client_id FROM edisc.matters WHERE id = m)))
$$;
CREATE FUNCTION client_held(c uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM edisc.legal_holds h
        WHERE h.released_at IS NULL
          AND (h.client_id = c OR h.matter_id IN (SELECT id FROM edisc.matters WHERE client_id = c)))
$$;
-- a hold placed on the client itself (its exports belong to no matter: a matter hold covers only
-- the archives its own jobs read, through their entries)
CREATE FUNCTION client_directly_held(c uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM edisc.legal_holds h WHERE h.released_at IS NULL AND h.client_id = c)
$$;
-- the matters whose evidence includes object e (the retention-extension ownership rule, reversed),
-- plus the client of an export whose archive it is
CREATE FUNCTION evidence_owner_matters(e uuid) RETURNS SETOF uuid
LANGUAGE sql STABLE SET search_path = pg_catalog, edisc, pg_temp AS $$
    WITH objs AS (  -- e itself and, for an archive, every entry object behind it
        SELECT e AS id
        UNION SELECT x.id FROM edisc.evidence_objects x WHERE x.archive_evidence_id = e)
    SELECT j.matter_id FROM objs o JOIN edisc.evidence_objects x ON x.id = o.id
        JOIN edisc.collection_jobs j ON j.id = x.job_id
    UNION SELECT j.matter_id FROM objs o JOIN edisc.items i ON i.evidence_object_id = o.id
        JOIN edisc.job_items ji ON ji.item_id = i.id JOIN edisc.collection_jobs j ON j.id = ji.job_id
    UNION SELECT j.matter_id FROM objs o JOIN edisc.evidence_objects x ON x.id = o.id
        JOIN edisc.renders r ON r.id = x.render_id JOIN edisc.collection_jobs j ON j.id = r.job_id
    UNION SELECT j.matter_id FROM objs o JOIN edisc.evidence_objects x ON x.id = o.id
        JOIN edisc.reports rp ON rp.id = x.report_id JOIN edisc.collection_jobs j ON j.id = rp.job_id
$$;
CREATE FUNCTION evidence_held(e uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM edisc.evidence_owner_matters(e) m WHERE edisc.matter_held(m))
        OR EXISTS (SELECT 1 FROM edisc.slack_exports x
                   WHERE x.evidence_object_id = e AND edisc.client_directly_held(x.client_id))
$$;
REVOKE ALL ON FUNCTION matter_held(uuid), client_held(uuid), client_directly_held(uuid), evidence_owner_matters(uuid), evidence_held(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION matter_held(uuid), client_held(uuid), client_directly_held(uuid), evidence_owner_matters(uuid), evidence_held(uuid) TO "{app}";
{MATTERS_GUARD}
{CLIENTS_GUARD}
"""


DOWNGRADE = """
CREATE OR REPLACE FUNCTION guard_matters() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.retention_until < OLD.retention_until OR NEW.tenant_id <> OLD.tenant_id THEN
        RAISE EXCEPTION 'matters: retention_until may only be extended' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NOT NULL AND NEW.closed_at IS NOT NULL
       AND (NEW.closed_at, NEW.closed_by) IS DISTINCT FROM (OLD.closed_at, OLD.closed_by) THEN
        RAISE EXCEPTION 'matters: reopen before closing again' USING ERRCODE = 'EA004';
    END IF;
    RETURN NEW;
END
$$;
CREATE OR REPLACE FUNCTION guard_clients() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.is_default <> OLD.is_default THEN
        RAISE EXCEPTION 'clients: tenant and default flag are immutable' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NOT NULL AND NEW.closed_at IS NOT NULL
       AND (NEW.closed_at, NEW.closed_by) IS DISTINCT FROM (OLD.closed_at, OLD.closed_by) THEN
        RAISE EXCEPTION 'clients: reopen before closing again' USING ERRCODE = 'EA004';
    END IF;
    RETURN NEW;
END
$$;
DROP FUNCTION IF EXISTS evidence_held(uuid);
DROP FUNCTION IF EXISTS evidence_owner_matters(uuid);
DROP FUNCTION IF EXISTS client_held(uuid);
DROP FUNCTION IF EXISTS client_directly_held(uuid);
DROP FUNCTION IF EXISTS matter_held(uuid);
DROP TABLE IF EXISTS legal_hold_jobs;
DROP INDEX IF EXISTS ix_evidence_objects_job_id_id;
DROP INDEX IF EXISTS ix_evidence_objects_archive_evidence_id;
DROP INDEX IF EXISTS ix_items_evidence_object_id;
DROP TABLE IF EXISTS legal_hold_objects;
DROP TABLE IF EXISTS legal_hold_release_requests;
DROP TABLE IF EXISTS legal_holds;
ALTER TABLE tenants DROP COLUMN IF EXISTS hold_cooling_off_days;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
