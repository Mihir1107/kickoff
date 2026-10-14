"""The mutation catalog: each entry breaks ONE protection with exact text edits and names the test that
must then fail. See README.md. ``tests/unit/test_mutation_catalog.py`` keeps every edit applicable.

Rounds (ADR 0015):
- ``s19-part-c``: render package download (§19.9; reconstructed from the recorded list, 20).
- ``s19-review``: content reads anchored, Content-Length, strict verifier, divergences (§19.11-14, 11).
- ``s20-natives``: oversized attachments as natives (§20/§21, 45; the retention break that round
  checked by hand is now one break per route, each caught by its own test).
- ``s21-review``: the review of §21 (name encoding, concurrent writers, the dummy pin).
- ``ci-heartbeat``: slice rendering off the event loop, so heartbeats keep flowing (CI run
  37412915073; ADR 0015 §23).
- ``m16-stream``: the report's lifecycle, custody stream, storage, retention and episodes (ADR 0018
  §9, §11, §13; M16 step 4).
- ``m16-model``: the collection report model and loader (ADR 0018 §4, §7, §8, §12; M16 step 1).
- ``m16-html``: the report HTML builder and its sanitiser (ADR 0018 §3.2, §4; M16 step 2).
- ``m16-pre3``: the fixes before step 3: marker elements, HTML-invalid code points replaced,
  `conversations.jsonl` above the cap, report goldens, CI shards cut after `-m` and verified from
  the collected set (ADR 0018 §21).
- ``m16-pdf``: the report PDF child and toolchain (ADR 0018 §5, §6; M16 step 3). The breaks that
  only a report image can catch (PDF goldens, the child's RLIMIT_AS in main, font isolation) are
  checked by hand in the image (ADR 0018 §22), not here: this harness runs on the host.
- ``m16-pre5``: the fixes before M16 step 5 (ADR 0018 §23-§26): lone surrogates hashed losslessly
  and stored/published valid at every stage, the glyph set in the report identity, the PDF rendered
  from the stored HTML (tamper), the production-install smoke job. Its ``image`` entries (tests under
  ``tests/unit/report_image``) run INSIDE the report-test image: ``make test-report-pdf`` runs
  ``scripts/mutation/run.py --kind image`` there (the CI ``report-pdf`` job).
- ``m16-api``: M16 step 5 (ADR 0018 §27): the job custody package download, the reports API
  (permissions, manual regeneration with who and why, audited downloads anchored before the first
  byte, re-hashed streams), the report package and ``edisc-verify`` (strictness, zipped job packages,
  the recomputation from the job chain).
- ``m16-pre6``: the fixes before M16 step 6 (ADR 0018 §28): hostile export values never drop data
  (names kept exactly; a conversation, team or workspace id, a ``ts`` or ``thread_ts`` holding a
  lone surrogate makes its units gaps with the reason), every unit fact in the job chain (details in
  unit events, unsettled units, the format decided per job) and ``units.jsonl`` recomputed byte for
  byte, the job package verifier strict and reading zips in place.
- ``m16-preview``: M16 step 6 (ADR 0018 §1.2, §10, §29): the preview route audited and anchored
  before the first byte with its CSP, refusals audited, nothing outside a scope served, the pure
  renderer's sanitiser and link allow-list, every message of the day on one page, the day-bounded
  loader and its thread roots, the renderer off the event loop.
- ``m16-export``: the export a job read, in its report (ADR 0018 §32): declared plan, detected tier,
  blind spots and findings recorded in `job_started`, stated (UNKNOWN when not recorded),
  recomputed by the verifier, shown in the HTML; export ingestion's orders under a tailored
  collation.
- ``m16-pre7``: the fixes before M16 step 7 (ADR 0018 §30): code-point order whatever the
  database's collation (the corpus run on a tailored ICU database, every ordered text column C),
  the clean verdict recomputed from the chain alone (a report may never state clean when the chain
  does not give clean), normalizer versions recorded in `job_started` and each batch, recomputed.
- ``m17-d11``: no error path echoes a request value (M17 step 0, ADR 0016 amendment, decision
  D11): the sanitised 422 body (no input/ctx, client-chosen loc keys, schema-only messages), the
  fixed 500 and its message-free log line, log paths without client segments, uvicorn's access log
  off, statement errors without parameters, recorded worker texts redacted on the way out, the
  route refusals that quoted the request.
- ``m17-logs``: the sweep of every test that asserts on log output (M17, before step 2): each one
  reads the production logging pipeline (``tests/logcapture.py``) and proves the lines under test
  reached it before asserting what they must not contain. Each break leaks a value into a log line
  that the test used to be blind to (``caplog``, an app without the access line, no logging at all).
- ``m17-sessions``: browser sessions (M17 step 2, ADR 0016 §1-§4, A1, A2.5): fixation, both expiries,
  revocation, the active principal, the throttled activity write, cookie flags, one credential per
  request, CSRF token and Origin, fetch metadata, single-use state, the login cookie handle, the
  nonce, every ``return_to`` rule, the re-authentication checks, the bounce page (in real browsers),
  deactivation revoking sessions, recent sign-in, the start throttle, the guard triggers.
- ``s24-pool``: fixed, warmed database pools and the churn report (ADR 0015 §24.13); the timing
  guard counting an idle sample when the loop's thread was measured busy, through the blocking-I/O
  guard's wrapper.
- ``m17-host-cookie``: the session cookie is host-only (``__Host-``, no ``Domain``) and a same-site
  request from a neighbouring subdomain is refused, both in real browsers (ADR 0016 §2).
- ``s24-loop``: the event-loop guard (timing and the OFF_LOOP registry in ``tests/conftest.py``,
  and the blocking-I/O guard ``edisc_core.loopblock``, §24.8), every CPU step moved off the loop, the
  render thread writing nothing (ADR 0015 §24).
"""

from __future__ import annotations

from dataclasses import dataclass

R = "packages/renderers/src/edisc_renderers/rsmf/"
C = "packages/custody/src/edisc_custody/"
W = "workers/collection/src/edisc_worker/"
EV = "packages/evidence/src/edisc_evidence/writer.py"
API = "apps/api/src/edisc_api/routes/renders.py"
JOBS = "apps/api/src/edisc_api/routes/jobs.py"
DUMMY = "packages/connectors/dummy/src/edisc_connector_dummy/spec.py"

UX = "tests/unit/renderers/test_externals.py"
UF = "tests/unit/custody/test_render_files.py"
UZ = "tests/unit/custody/test_zipwriter.py"
ULT = "tests/unit/custody/test_loop_turns.py"
UR = "tests/unit/custody/test_rsmf_check.py"
UD = "tests/unit/dummy/test_oracle_and_determinism.py"
IN = "tests/integration/renders/test_render_natives.py"
CM = "tests/integration/renders/test_render_crash_matrix.py"
AN = "tests/integration/api/test_render_natives.py"
AP = "tests/integration/api/test_render_packages.py"
AC = "tests/integration/api/test_content_reads_anchored.py"
CN = "tests/integration/custody/test_render_package_natives.py"
CP = "tests/integration/custody/test_render_package.py"
LG = "packages/core/src/edisc_core/loopguard.py"
IPOOL = "tests/integration/db/test_pool.py"
DC = "packages/connectors/dummy/src/edisc_connector_dummy/connector.py"
ULG = "tests/unit/core/test_loopguard.py"
LB = "packages/core/src/edisc_core/loopblock.py"
ULB = "tests/unit/core/test_loopblock.py"
UJ = "tests/unit/core/test_jsonstream.py"
UE = "tests/unit/renderers/test_eml.py"
UT = "tests/unit/renderers/test_render_thread_writes_nothing.py"
UDE = "tests/unit/dummy/test_epochs_failures_policy.py"
CORPUS = "tests/integration/corpus/test_corpus.py"
PL = "tests/integration/pipeline/test_pipeline.py"
CL = "tests/integration/custody/test_custody_log.py"
RS = "tests/integration/renders/test_render_store.py"
NS = "packages/normalizer/src/edisc_normalizer/store.py"
RM = "packages/renderers/src/edisc_renderers/report/model.py"
RH = "packages/renderers/src/edisc_renderers/report/html.py"
UH = "tests/unit/renderers/test_report_html.py"
RL = "workers/collection/src/edisc_worker/report_loader.py"
UM = "tests/unit/renderers/test_report_model.py"
RT = "tests/integration/report/test_report_model.py"
RR = "tests/integration/renders/test_report_renders.py"
WR = "tests/integration/worker/test_report_cases.py"
AE = "tests/integration/api/test_report_export.py"
RW = "workers/collection/src/edisc_worker/reports.py"
RLC = "tests/integration/reports/test_report_lifecycle.py"
RCC = "tests/integration/report/test_conversations_cap.py"
UG = "tests/unit/renderers/test_report_golden.py"
UMU = "tests/unit/renderers/test_report_model.py"
USH = "tests/unit/test_ci_shards.py"
ICF = "tests/integration/conftest.py"
SH = "scripts/ci_shard.py"
RP = "workers/collection/src/edisc_worker/report_pdf.py"
VER = "workers/collection/src/edisc_worker/versions.py"
UPR = "tests/unit/report_pdf/test_pdf_child_runner.py"
UTM = "tests/unit/report_pdf/test_toolchain_manifest.py"
RWF = "tests/integration/reports/test_report_workflow.py"
ROP = "tests/integration/reports/test_report_operations.py"
CAN = "packages/core/src/edisc_core/canonical.py"
NSL = "packages/normalizer/src/edisc_normalizer/slack.py"
NMO = "packages/normalizer/src/edisc_normalizer/model.py"
RLO = "workers/collection/src/edisc_worker/render_loader.py"
PKG = "packages/custody/src/edisc_custody/package.py"
PIPE = "workers/collection/src/edisc_worker/pipeline.py"
LAY = "packages/connectors/slack_export/src/edisc_connector_slack_export/layout.py"
CON = "workers/collection/src/edisc_worker/contracts.py"
FONTS_CONF = "deploy/report-image/fonts.conf"
ULS = "tests/unit/core/test_lone_surrogates.py"
UNS = "tests/unit/normalizer/test_slack_normalizer.py"
URE = "tests/unit/renderers/test_render.py"
ULA = "tests/unit/connectors/test_slack_export_layout.py"
UGI = "tests/unit/renderers/test_report_glyph_identity.py"
RLS = "tests/integration/report/test_lone_surrogates.py"
RPS = "tests/integration/reports/test_report_pdf_from_stored_html.py"
IPG = "tests/unit/report_image/test_pdf_goldens.py"
IPD = "tests/unit/report_image/test_pdf.py"
IFI = "tests/unit/report_image/test_font_isolation.py"
PROD = "tests/integration/production/test_production_install.py"
JP = "apps/api/src/edisc_api/routes/job_package.py"
REP = "apps/api/src/edisc_api/routes/reports.py"
AUTHZ = "apps/api/src/edisc_api/authz.py"
RPK = "packages/custody/src/edisc_custody/report_package.py"
CLI = "packages/custody/src/edisc_custody/cli.py"
JEX = "packages/custody/src/edisc_custody/job_export.py"
AJP = "tests/integration/api/test_job_package.py"
ARP = "tests/integration/api/test_reports.py"
PS = "packages/custody/src/edisc_custody/package_source.py"
PA = "packages/custody/src/edisc_custody/package_archives.py"
RC = "packages/renderers/src/edisc_renderers/report/recompute.py"
EXW = "workers/collection/src/edisc_worker/exports.py"
CONX = "packages/connectors/slack_export/src/edisc_connector_slack_export/connector.py"
TP = "tests/integration/custody/test_package.py"
AEP = "tests/integration/api/test_export_package.py"
AEC = "tests/integration/api/test_export_collection.py"
ACE = "tests/integration/api/test_corpus_export.py"
RUF = "tests/integration/report/test_unit_facts.py"
URC = "tests/unit/renderers/test_report_recompute.py"
PRV = "apps/api/src/edisc_api/routes/preview.py"
PVH = "packages/renderers/src/edisc_renderers/preview/html.py"
PVW = "workers/collection/src/edisc_worker/preview.py"
APV = "tests/integration/api/test_preview.py"
UPV = "tests/unit/renderers/test_preview.py"
M33 = "packages/db/migrations/versions/0033_unit_facts_and_hostile_ids.py"
M35 = "packages/db/migrations/versions/0035_code_point_order.py"
NST = "packages/normalizer/src/edisc_normalizer/store.py"
CLI = "packages/custody/src/edisc_custody/cli.py"
CCO = "tests/integration/corpus/test_collation.py"
DCO = "tests/integration/db/test_collation.py"
URV = "tests/unit/renderers/test_report_verdict.py"
UEX = "tests/unit/renderers/test_report_export.py"
SE = "apps/api/src/edisc_api/safe_errors.py"
APPM = "apps/api/src/edisc_api/app.py"
ERRS = "apps/api/src/edisc_api/errors.py"
SESS = "packages/db/src/edisc_db/session.py"
USE = "tests/unit/api/test_safe_errors.py"
NOE = "tests/integration/api/test_no_echo.py"
DBS = "tests/integration/db/test_statement_errors.py"
ENV = "packages/core/src/edisc_core/envelope.py"
CONN = "apps/api/src/edisc_api/routes/connections.py"
ACO = "tests/integration/api/test_connections.py"
UKMS = "tests/unit/core/test_kms_envelope.py"
AUTH = "apps/api/src/edisc_api/auth.py"
AUR = "apps/api/src/edisc_api/routes/auth.py"
SESP = "apps/api/src/edisc_api/sessions.py"
OIDC = "apps/api/src/edisc_api/oidc.py"
ADM = "apps/api/src/edisc_api/routes/admin.py"
SES = "tests/integration/api/test_sessions.py"
SESB = "tests/integration/api/test_sessions_browser.py"
USP = "tests/unit/api/test_session_primitives.py"
DSG = "tests/integration/db/test_session_guards.py"
M36 = "packages/db/migrations/versions/0036_browser_sessions.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    round: str
    path: str  # repo-relative file to break
    edits: tuple[tuple[str, str], ...]  # (old, new): each old must occur exactly once
    test: str  # repo-relative test file
    select: str = ""  # pytest -k expression

    @property
    def kind(self) -> str:
        if self.test.startswith("tests/unit/report_image/"):
            return (
                "image"  # runs inside the report-test image (scripts/mutation/run.py --kind image)
            )
        return "integration" if self.test.startswith("tests/integration/") else "unit"


def m(name: str, rnd: str, path: str, old: str, new: str, test: str, select: str = "") -> Mutation:
    return Mutation(name, rnd, path, ((old, new),), test, select)


_ANCHOR = "    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)\n"

CATALOG: list[Mutation] = [
    # ------------------------------------------------------------------ §19 part C (reconstructed)
    m("pkg_not_anchored", "s19-part-c", API,
      _ANCHOR + "\n    async def body() -> AsyncIterator[bytes]:\n        members =",
      "\n    async def body() -> AsyncIterator[bytes]:\n        members =", AP, "anchored_before"),
    m("pkg_unsealed_served", "s19-part-c", API,
      "        if row.seal_storage_key is None:\n            raise ApiError(",
      "        if False:\n            raise ApiError(", AP, "not_sealed"),
    m("pkg_wrong_permission", "s19-part-c", API,
      "        row = await _authorize_render(s, caller, P.EXPORT_READ, render_id)\n        if row.seal_storage_key is None:",
      "        row = await _authorize_render(s, caller, P.CUSTODY_READ, render_id)\n        if row.seal_storage_key is None:",
      AP, "only_matter_managers"),
    m("pkg_abort_not_integrity", "s19-part-c", API,
      "    integrity = isinstance(exc, (PackageIntegrityError, ZipSizeError))", "    integrity = False",
      AP, "differs_from_its_record_aborts"),
    m("pkg_abort_not_recorded", "s19-part-c", API,
      "            await _package_aborted(res, caller, rid, row, plan, base, exc)\n", "",
      AP, "differs_from_its_record_aborts"),
    m("stream_jsonl_unchecked", "s19-part-c", C + "render_export.py",
      '        if (digest.hexdigest(), count, size) != (spec["sha256"], spec["lines"], spec["bytes"]):',
      "        if False:", CP, "records_that_change_after_the_plan"),
    m("stream_object_unchecked", "s19-part-c", C + "render_export.py",
      "        if (digest.hexdigest(), seen) != (sha256, size):\n            raise PackageIntegrityError(",
      "        if False:\n            raise PackageIntegrityError(", AP, "differs_from_its_record_aborts"),
    m("stream_output_registry", "s19-part-c", C + "render_export.py",
      "            or row.registry_version != row.version_id\n        ):\n            raise",
      "            or row.registry_version != row.version_id\n        ) and False:\n            raise",
      CP, "output_whose_registry_disagrees"),
    m("pkg_not_deterministic", "s19-part-c", C + "render_export.py",
      '        "outputs_included": outputs == "embed",',
      '        "outputs_included": outputs == "embed",\n        "nonce": str(uuid.uuid4()),',
      CP, "two_downloads_are_byte_identical"),
    m("verify_object_hash", "s19-part-c", C + "render_package.py",
      "        if (hashlib.sha256(data).hexdigest(), len(data)) != (sha, size):", "        if False:",
      CP, "altered_anchor_object"),
    m("verify_manifest_files", "s19-part-c", C + "render_package.py",
      '            report.errors.append(f"{name}: does not match manifest (modified after export)")',
      "            pass", CP, "edit_without_fixing_the_manifest"),
    m("verify_unlisted_files", "s19-part-c", C + "render_package.py",
      '            report.errors.append(f"{extra}: not part of this package")', "            pass",
      CP, "missing_object_or_a_planted_file"),
    m("reader_duplicate_names", "s19-part-c", C + "package_source.py",
      "            if key in folded:\n                raise ArchiveError",
      "            if False:\n                raise ArchiveError", UZ, "refuses_duplicates"),
    m("writer_no_descriptor_flag", "s19-part-c", C + "zipwriter.py",
      "_FLAGS = 0x0800 | 0x0008", "_FLAGS = 0x0800", UZ, "round_trip_and_fixed_metadata"),
    m("writer_no_crc", "s19-part-c", C + "zipwriter.py",
      "            crc = zlib.crc32(chunk, crc)\n", "", UZ, "round_trip_and_fixed_metadata"),
    m("writer_no_zip64_entry", "s19-part-c", C + "zipwriter.py",
      "def needs_zip64(size: int) -> bool:\n    return size >= MAX32",
      "def needs_zip64(size: int) -> bool:\n    return False", UZ, "more_than_4_gib"),
    m("writer_no_zip64_offset", "s19-part-c", C + "zipwriter.py",
      "    if offset >= MAX32:\n        values.append(offset)",
      "    if False:\n        values.append(offset)", UZ, "more_than_4_gib"),
    m("writer_no_zip64_count", "s19-part-c", C + "zipwriter.py",
      "    if count >= MAX16 or cd_size >= MAX32 or cd_offset >= MAX32:",
      "    if cd_size >= MAX32 or cd_offset >= MAX32:", UZ, "more_than_65535"),
    m("writer_more_bytes_passed_on", "s19-part-c", C + "zipwriter.py",
      "            if size > m.size:\n                raise ZipSizeError",
      "            if False:\n                raise ZipSizeError", UZ, "more_bytes_are_refused"),
    m("writer_fewer_bytes", "s19-part-c", C + "zipwriter.py",
      "        if size != m.size:\n            raise ZipSizeError",
      "        if False:\n            raise ZipSizeError", UZ, "differs_from_its_declared_size"),
    # ------------------------------------------------------------------ §19 review round
    m("evidence_read_not_anchored", "s19-review", JOBS,
      "    # committed AND anchored (forced, checked to cover the read) before any byte\n" + _ANCHOR,
      "", AC, "evidence_content"),
    m("render_file_read_not_anchored", "s19-review", API,
      _ANCHOR + "    writer = EvidenceWriter(res.sessions, res.s3, res.settings)\n\n"
      "    async def body() -> AsyncIterator[bytes]:\n        digest, size = hashlib.sha256(), 0\n"
      "        async for chunk in writer.open(\n            tenant_id=caller.tenant_id, evidence_id=f.evidence_object_id",
      "    writer = EvidenceWriter(res.sessions, res.s3, res.settings)\n\n"
      "    async def body() -> AsyncIterator[bytes]:\n        digest, size = hashlib.sha256(), 0\n"
      "        async for chunk in writer.open(\n            tenant_id=caller.tenant_id, evidence_id=f.evidence_object_id",
      AC, "render_file"),
    m("content_length_wrong", "s19-review", API,
      '"content-length": str(plan.zip_size),', '"content-length": str(plan.zip_size + 1),',
      AP, "byte_identical"),
    m("sizer_descriptor", "s19-review", C + "zipwriter.py",
      "        descriptor = _DESCRIPTOR64.size if needs_zip64(size) else _DESCRIPTOR.size",
      "        descriptor = _DESCRIPTOR.size", UZ, "more_than_4_gib"),
    m("tolerate_in_zip", "s19-review", C + "render_package.py",
      "    tolerate = tolerate_os_metadata and not isinstance(source, ZipSource)",
      "    tolerate = tolerate_os_metadata", CP, "never_applies_to_a_zip"),
    m("tolerated_not_listed", "s19-review", C + "render_package.py",
      "            report.tolerated.append(extra)", "            pass", CP, "tolerated_and_listed"),
    m("tolerate_too_much", "s19-review", C + "package_source.py",
      '        or base.startswith("._")', '        or base.startswith(".")', CP, "anything_else"),
    m("divergence_not_recorded", "s19-review", API,
      "        if plan.anchor_divergences:", "        if False:", AP, "anchor_divergence"),
    m("divergence_no_hash_check", "s19-review", C + "render_export.py",
      "        elif (row.sha256, row.size_bytes) != (obj.sha256, obj.size):", "        elif False:",
      AP, "anchor_divergence and hash_mismatch"),
    m("divergence_no_missing_scan", "s19-review", C + "render_export.py",
      "        if (key, row.version_id) not in listed:", "        if False:",
      AP, "anchor_divergence and missing_object"),
    m("divergence_no_delete_marker", "s19-review", C + "render_export.py",
      '            divergences.append(_divergence("delete_marker", v.key, v.version_id, row))\n', "",
      AP, "anchor_divergence and delete_marker"),
    # ------------------------------------------------------------------ §20 natives: pure layers
    m("threshold_inclusive", "s20-natives", R + "render.py",
      "if a.size > options.external_over_bytes", "if a.size >= options.external_over_bytes", UX, "threshold"),
    m("no_structural_rule", "s20-natives", R + "render.py",
      "while sizer(entries).needs_zip64():", "while False:", UX, "zip64_boundary or largest_first"),
    m("ties_not_by_id", "s20-natives", R + "render.py",
      "key=lambda a: (-a.size, a.file_id)", "key=lambda a: (-a.size, -ord(a.file_id[-1]))", UX, "ties"),
    m("placeholder_crlf", "s20-natives", R + "render.py",
      'return ("\\n".join(lines) + "\\n").encode("utf-8")',
      'return ("\\r\\n".join(lines) + "\\r\\n").encode("utf-8")', UX, "pinned"),
    m("placeholder_no_escape", "s20-natives", R + "render.py",
      '_ESCAPED_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})', '_ESCAPED_CATEGORIES = frozenset({"Cs"})',
      UX, "pinned"),
    m("native_streamed_inline", "s20-natives", R + "render.py",
      "if isinstance(outcome, FileAttachment) and outcome.file_id in externals:\n                text = _external_text",
      "if False:\n                text = _external_text", UX, "never_reads"),
    m("entry_budget_off_by_one", "s20-natives", R + "slicing.py",
      "if current and total + need > budget:", "if current and total + need >= budget:",
      UX, "exactly_at_the_entry_limit"),
    m("entry_root_not_counted", "s20-natives", R + "slicing.py",
      "        if root is not None and root not in present:\n            need += entries_of(root)\n", "",
      UX, "context_root"),
    m("entry_split_unwired", "s20-natives", R + "render.py",
      "budget = MAX_PART_ENTRIES - FIXED_ENTRIES", "budget = 10**9", UX, "entry"),
    m("zip64_no_dir_end", "s20-natives", C + "zipwriter.py",
      "            or self.offset + self.cd_size >= MAX32  # the directory's end\n", "", UZ, "needs_zip64"),
    m("zip64_count_off_by_one", "s20-natives", C + "zipwriter.py",
      "or self.count >= MAX16", "or self.count > MAX16", UZ, "needs_zip64"),
    m("reconcile_missing", "s20-natives", R + "reconcile.py",
      "        if missing:\n            raise", "        if False:\n            raise", UX, "reconciler_checks"),
    m("reconcile_unreferenced", "s20-natives", R + "reconcile.py",
      "        if extra:\n            raise", "        if False:\n            raise", UX, "reconciler_checks"),
    m("reconcile_size", "s20-natives", R + "reconcile.py",
      "        if wrong:\n            raise", "        if False:\n            raise", UX, "reconciler_checks"),
    m("reconcile_unplanned", "s20-natives", R + "reconcile.py",
      "if (fid, digest) not in planned:", "if False:", UX, "not_planned"),
    m("native_ords_unsorted", "s20-natives", C + "render_files.py",
      "any(b <= a for a, b in pairwise(ords))", "any(b < a for a, b in pairwise(ords))", UF, "native_records"),
    m("chain_no_batch_root", "s20-natives", C + "chain.py",
      'if root != payload.get("natives_root"):', "if False:", UF, "natives_are_verified"),
    m("chain_no_first_reference", "s20-natives", C + "chain.py",
      "if not first_file <= ords[0] < first_file + file_count:", "if False:", UF, "first_referenced"),
    m("chain_no_duplicate", "s20-natives", C + "chain.py",
      'if view["sha256"] in self._native_shas:', "if False:", UF, "first_referenced"),
    m("chain_leaf_unversioned", "s20-natives", C + "chain.py",
      "root = files_root(files, file_fields(self._renderer_version))", "root = files_root(files)",
      UF, "file_leaf_follows"),
    m("chain_no_total_root", "s20-natives", C + "chain.py",
      'if payload.get("natives_root") != total:', "if False:", UF, "natives_are_verified"),
    m("chain_old_render_natives", "s20-natives", C + "chain.py",
      'elif natives or "natives_root" in payload:', "elif False:", UF, "older_render"),
    m("rsmf_placeholder_sha", "s20-natives", C + "rsmf_check.py",
      'fields["sha256"] != sha\n', "False\n", UR, "disagrees or field"),
    m("rsmf_placeholder_layout", "s20-natives", C + "rsmf_check.py",
      'or "\\r" in text', "or False", UR, "disagrees"),
    m("rsmf_missing_placeholder", "s20-natives", C + "rsmf_check.py",
      "if entry is None or entry.uncompressed_size > _PLACEHOLDER_MAX:",
      "if entry is not None and entry.uncompressed_size > _PLACEHOLDER_MAX:", UR, "without_a_placeholder"),
    m("rsmf_line_width", "s20-natives", C + "rsmf_check.py",
      'if b"\\r\\n" in line[:-2] or not line.endswith(b"\\r\\n"):', "if False:", UR, "crlf"),
    # ------------------------------------------------------------------ §20 natives: stack
    m("copy_not_verified", "s20-natives", EV,
      "        if (digest, got) != (sha256, size):\n            raise EvidenceIntegrityError(",
      "        if False:\n            raise EvidenceIntegrityError(", IN, "never_completed"),
    m("copy_versions_unchecked", "s20-natives", EV,
      "        if len(versions) > 1:\n            raise EvidenceIntegrityError(f\"{key}: {len(versions)} versions of one native\")",
      "        if False:\n            raise EvidenceIntegrityError(f\"{key}: {len(versions)} versions of one native\")",
      IN, "incident"),
    m("copy_not_pinned", "s20-natives", EV, '                "VersionId": source_version_id,\n', "", IN, "pinned"),
    m("open_upload_left", "s20-natives", EV,
      "            await self._abort_open_uploads(key, row.upload_id)\n", "", IN, "sigkill and parts_copied"),
    m("final_natives_unchecked", "s20-natives", W + "renders.py",
      '            out.reconciler.check_natives((n["sha256"], n["size"]) for n in natives)\n', "",
      IN, "missing_from"),
    m("batch_natives_root_empty", "s20-natives", W + "renders.py",
      '"natives_root": natives_root(natives, first_native),', '"natives_root": natives_root([], first_native),',
      IN, "copied_once"),
    m("verify_chain_no_natives", "s20-natives", C + "log.py",
      "files.get(row.id), natives.get(row.id)\n", "files.get(row.id), None\n", IN,
      "altered_native_record"),
    m("retention_job_route", "s20-natives", C + "retention_extension.py",
      '"   JOIN evidence_objects e ON e.job_id = j.id"',
      "\"   JOIN evidence_objects e ON e.job_id = j.id AND e.storage_key NOT LIKE '%/natives/%'\"",
      IN, "only-via-job_id"),
    m("retention_render_route", "s20-natives", C + "retention_extension.py",
      '"   JOIN evidence_objects e ON e.render_id = r.id"\n',
      "\"   JOIN evidence_objects e ON e.render_id = r.id AND e.storage_key NOT LIKE '%/natives/%'\"\n",
      IN, "only-via-render_id"),
    m("pkg_reference_mismatch", "s20-natives", C + "render_package.py",
      "        if named != listed:", "        if False:", CN, "rewritten_reference"),
    m("pkg_unlisted_native", "s20-natives", C + "render_package.py",
      "            if known is None:", "            if False:", CN, "dropped_from"),
    m("pkg_native_bytes", "s20-natives", C + "render_package.py",
      '        if (digest, size) != (sha, n.get("size")):', "        if False:", CN, "altered_native"),
    m("pkg_stray_native", "s20-natives", C + "render_package.py",
      "        allowed |= {f\"natives/{n.get('sha256')}\" for n in natives}",
      '        allowed |= {x for x in source.names() if x.startswith("natives/")}', CN, "stray"),
    m("pkg_native_not_required", "s20-natives", C + "render_package.py",
      '            report.errors.append(f"native {sha}: not in the package and not supplied")\n            continue',
      "            continue", CN, "missing_native"),
    m("stream_native_registry", "s20-natives", C + "render_export.py",
      "            raise PackageIntegrityError(name, \"the registry disagrees with the render's record\")\n"
      "        yield _object_member(\n            s3, bucket, name, row.storage_key, row.version_id, row.sha256, row.size_bytes\n        )\n\n\n",
      "            pass\n        yield _object_member(\n            s3, bucket, name, row.storage_key, row.version_id, row.sha256, row.size_bytes\n        )\n\n\n",
      AN, "aborts_the_package"),
    m("api_native_not_anchored", "s20-natives", API,
      _ANCHOR + "    writer = EvidenceWriter(res.sessions, res.s3, res.settings)\n\n"
      "    async def body() -> AsyncIterator[bytes]:\n        digest, size = hashlib.sha256(), 0\n"
      "        async for chunk in writer.open(\n            tenant_id=caller.tenant_id, evidence_id=n.evidence_object_id",
      "    writer = EvidenceWriter(res.sessions, res.s3, res.settings)\n\n"
      "    async def body() -> AsyncIterator[bytes]:\n        digest, size = hashlib.sha256(), 0\n"
      "        async for chunk in writer.open(\n            tenant_id=caller.tenant_id, evidence_id=n.evidence_object_id",
      AN, "anchored_before"),
    m("api_native_not_rehashed", "s20-natives", API,
      "        if (digest.hexdigest(), size) != (n.sha256, n.size_bytes):", "        if False:",
      AN, "aborts_the_download"),
    m("api_native_wrong_permission", "s20-natives", API,
      "        row = await _authorize_render(s, caller, P.EXPORT_READ, render_id)\n"
      "        if row.status != \"completed\":\n            raise conflict(f\"the render is {row.status}: only completed renders are downloaded\")\n"
      "        n = (",
      "        row = await _authorize_render(s, caller, P.CUSTODY_READ, render_id)\n"
      "        if row.status != \"completed\":\n            raise conflict(f\"the render is {row.status}: only completed renders are downloaded\")\n"
      "        n = (",
      AN, "who_may"),
    m("api_threshold_unbounded", "s20-natives", API,
      "default=EXTERNAL_OVER_BYTES, ge=EXTERNAL_OVER_MIN, le=EXTERNAL_OVER_MAX, strict=True",
      "default=EXTERNAL_OVER_BYTES", AN, "threshold"),
    # ------------------------------------------------------------------ §21 review
    m("name_backslash_unescaped", "s21-review", R + "render.py",
      '        "\\\\\\\\"\n        if c == "\\\\"\n        else ', "        ", UX, "round_trip or different"),
    m("decoder_accepts_raw_controls", "s21-review", C + "rsmf_check.py",
      "            if unicodedata.category(c) in _ESCAPED_CATEGORIES:\n                raise RsmfCheckError",
      "            if False:\n                raise RsmfCheckError", UX, "one_encoding"),
    m("decoder_accepts_needless_escapes", "s21-review", C + "rsmf_check.py",
      "            if unicodedata.category(ch) not in _ESCAPED_CATEGORIES:", "            if False:",
      UX, "one_encoding"),
    m("verifier_ignores_display", "s21-review", C + "rsmf_check.py",
      'if len(want) != 1 or (name not in want and not (name == "" and want == {fid})):', "if False:",
      UR, "display"),
    m("native_copy_unlocked", "s21-review", EV,
      "        async with self._content_lock(key):\n            try:\n"
      "                async with asyncio.timeout(self._settings.evidence_copy_timeout_seconds):\n"
      "                    return await self._native_locked(",
      "        async with asyncio.timeout(None):\n            try:\n"
      "                async with asyncio.timeout(self._settings.evidence_copy_timeout_seconds):\n"
      "                    return await self._native_locked(", IN, "two_writers"),
    m("production_write_unlocked", "s21-review", EV,
      "        async with self._content_lock(key):\n            return await self._write_output_locked(\n"
      '                tenant_id, job_id, _Owner("production"',
      "        async with asyncio.timeout(None):\n            return await self._write_output_locked(\n"
      '                tenant_id, job_id, _Owner("production"',
      IN, "executors_render"),
    m("no_relist_after_abort", "s21-review", EV,
      "            await self._abort_open_uploads(key, row.upload_id)\n"
      "            versions = [v for v, marker in await self._versions(key) if not marker]\n",
      "            await self._abort_open_uploads(key, row.upload_id)\n", IN, "lost_its_lock"),
    m("dummy_default_unpinned", "s21-review", DUMMY,
      "file_size_min: int = Field(default=200, ge=0)", "file_size_min: int = Field(default=201, ge=0)",
      UD, "golden_digest"),
    # ------------------------------------------------------------------ CI run 37412915073 (§23)
    m("slice_render_on_event_loop", "ci-heartbeat", W + "render_store.py",
      "    return await asyncio.to_thread(_render_slice_blocking, inp, options, hooks)",
      "    return _render_slice_blocking(inp, options, hooks)", CM, "keeps_heartbeating"),
    # ------------------------------------------------------------------ the loop guard (§24)
    # the timing guard alone (render_slice itself is not in OFF_LOOP): the 10,001-event slice
    m("guard_slice_render_on_loop", "s24-loop", W + "render_store.py",
      "    return await asyncio.to_thread(_render_slice_blocking, inp, options, hooks)",
      "    return render_slice(inp, options)", CORPUS, "cap_10001"),
    m("render_thread_writes", "s24-loop", W + "render_store.py",
      "    return render_slice(inp, options)",
      '    open("/dev/null", "w").close()\n    return render_slice(inp, options)', UT),
    m("guard_blames_tests", "s24-loop", LG, 'return "product", where', 'return "test", where', ULG),
    m("guard_gc_not_excused", "s24-loop", LG,
      "if (late - paused) * 1000 <= self.threshold_ms:", "if False:", ULG, "garbage"),
    m("guard_selector_not_idle", "s24-loop", LG, 'return "idle", f"', 'return "product", f"',
      ULG, "selector"),
    m("guard_counts_idle_stalls", "s24-loop", LG,
      "return (self.cpu_ms - self.gc_ms) >= BUSY_SHARE * (self.total_ms - self.gc_ms)",
      "return True", ULG, "without_cpu"),
    m("guard_scram_counted", "s24-loop", LG, "    if _driver_read_callback(stack):\n",
      "    if False:\n", ULG, "scram"),
    m("guard_follows_sync_drivers", "s24-loop", LG,
      "        if not f.f_code.co_flags & (_ASYNC | inspect.CO_GENERATOR):\n            break\n", "",
      ULG, "synchronously"),
    m("jsonstream_no_yield", "s24-loop", "packages/core/src/edisc_core/jsonstream.py",
      "        await asyncio.sleep(0)\n", "", UJ, "turn"),
    m("zipwriter_no_yield", "s24-loop", C + "zipwriter.py",
      "        await asyncio.sleep(0)\n", "", UZ, "ready_members"),
    m("zipwriter_no_chunk_yield", "s24-loop", C + "zipwriter.py",
      "            yield chunk\n            await asyncio.sleep(0)  # a huge member from a ready source must not block the loop\n",
      "            yield chunk\n", UZ, "single_large_member"),
    m("entry_reader_no_yield", "s24-loop", C + "archive.py",
      "        await asyncio.sleep(0)\n        if inflater is None:", "        if inflater is None:",
      ULT, "turn_per_chunk"),
    m("sync_driver_refuses_turns", "s24-loop", C + "package_source.py",
      "        while steps.send(None) is None:\n            pass\n",
      "        if steps.send(None) is None:\n            pass\n", ULT, "drivers"),
    m("rsmf_driver_refuses_turns", "s24-loop", C + "rsmf_check.py",
      "        while steps.send(None) is None:\n            pass\n",
      "        if steps.send(None) is None:\n            pass\n", ULT, "drivers"),
    m("api_routes_built_on_the_loop", "s24-loop", "apps/api/src/edisc_api/app.py",
      "        await asyncio.to_thread(warm_routes, app)  # never on the loop at the first requests\n",
      "", "tests/unit/api/test_warm_routes.py", "startup"),
    m("envelope_no_yield", "s24-loop", R + "eml.py",
      "            if out:\n                yield out\n            await asyncio.sleep(0)\n",
      "            if out:\n                yield out\n", UE, "turn"),
    m("envelope_unbounded_feed", "s24-loop", R + "eml.py",
      "        for start in range(0, len(chunk), _B64_FEED):  # bound the base64 CPU per loop turn\n",
      "        for start in range(0, len(chunk), 1 << 40):  # bound the base64 CPU per loop turn\n",
      UE, "bounds_a_single_large"),
    # each pure function the product runs in a thread, called on the loop instead (OFF_LOOP)
    m("dummy_page_on_loop", "s24-loop", DC, "result: T = await asyncio.to_thread(respond)",
      "result: T = respond()", UDE, "oracle_model"),
    m("dummy_plan_on_loop", "s24-loop", DC,
      "batches = await asyncio.to_thread(self.plan, conn, unit_, scope)",
      "batches = self.plan(conn, unit_, scope)", UDE, "oracle_model"),
    m("fragment_hash_on_loop", "s24-loop", W + "pipeline.py",
      "await asyncio.to_thread(messages_fragment_hash, batch.body, dialect)",
      "messages_fragment_hash(batch.body, dialect)", PL, "three_epochs"),
    m("fallback_fragment_hash_on_loop", "s24-loop", W + "pipeline.py",
      "fragment = await asyncio.to_thread(messages_fragment_hash, body)",
      "fragment = messages_fragment_hash(body)", PL, "pre_0014_fallback"),
    m("absence_on_loop", "s24-loop", W + "pipeline.py",
      "absent = await asyncio.to_thread(  # one event per missing message: CPU\n                    finalize_unit,\n",
      "absent = finalize_unit(\n", PL, "three_epochs"),
    m("file_refs_on_loop", "s24-loop", W + "pipeline.py",
      "refs = await asyncio.to_thread(file_refs, body, dialect, select)",
      "refs = file_refs(body, dialect, select)", PL, "unavailable_files_are_recorded"),
    m("directory_subjects_on_loop", "s24-loop", W + "pipeline.py",
      "subjects = await asyncio.to_thread(directory_page_subjects, batch.body, ctx=ctx)",
      "subjects = directory_page_subjects(batch.body, ctx=ctx)", PL, "three_epochs"),
    m("directory_normalize_on_loop", "s24-loop", W + "pipeline.py",
      "result = await asyncio.to_thread(\n                    normalize_directory_page, batch.body,",
      "result = normalize_directory_page(\n                    batch.body,", PL, "three_epochs"),
    m("message_subjects_on_loop", "s24-loop", W + "pipeline.py",
      "subjects = await asyncio.to_thread(\n                    message_page_subjects, batch.body,"
      " ctx=ctx, select=batch.select\n                )",
      "subjects = message_page_subjects(batch.body, ctx=ctx, select=batch.select)", PL,
      "three_epochs"),
    m("normalize_on_loop", "s24-loop", W + "pipeline.py",
      "result = await asyncio.to_thread(\n                    normalize_messages_page,\n",
      "result = normalize_messages_page(\n", PL, "three_epochs"),
    m("access_restored_on_loop", "s24-loop", W + "pipeline.py",
      "await asyncio.to_thread(\n                        access_restored, ctx=ctx,",
      "access_restored(\n                        ctx=ctx,", PL, "three_epochs"),
    m("derivation_encoding_on_loop", "s24-loop", "packages/normalizer/src/edisc_normalizer/store.py",
      "docs, hashes = await asyncio.to_thread(_encode_derivations, ",
      "docs, hashes = _encode_derivations(", PL, "three_epochs"),
    m("verify_page_on_loop", "s24-loop", C + "log.py",
      "await asyncio.to_thread(_verify_page, verifier,", "_verify_page(verifier,", CL,
      "periodic_and_final"),
    m("index_rows_on_loop", "s24-loop", W + "render_loader.py",
      "source = await asyncio.to_thread(_index_rows, rows, scope, conversation_id, out)",
      "source = _index_rows(rows, scope, conversation_id, out)", RS, "locked_productions"),
    m("derivation_check_on_loop", "s24-loop", W + "render_loader.py",
      "await asyncio.to_thread(_check_derivations, found)", "_check_derivations(found)", RS,
      "locked_productions"),
    m("page_check_on_loop", "s24-loop", W + "render_loader.py",
      "await asyncio.to_thread(_check_page, data, by_evidence[evidence_id])",
      "_check_page(data, by_evidence[evidence_id])", RS, "locked_productions"),
    m("order_items_on_loop", "s24-loop", NS,
      "await asyncio.to_thread(_order_items, items, tenant, source)",
      "_order_items(items, tenant, source)", PL, "three_epochs"),
    m("assign_ids_on_loop", "s24-loop", NS,
      "await asyncio.to_thread(_assign_ids, ordered, keys, item_ids, tenant, source)",
      "_assign_ids(ordered, keys, item_ids, tenant, source)", PL, "three_epochs"),
    m("item_rows_on_loop", "s24-loop", NS, "rows = await asyncio.to_thread(\n        _item_rows, ",
      "rows = _item_rows(\n        ", PL, "three_epochs"),
    m("batch_keys_on_loop", "s24-loop", W + "pipeline.py",
      "await asyncio.to_thread(_by_key, items, ctx.tenant_id, ctx.source)",
      "_by_key(items, ctx.tenant_id, ctx.source)", PL, "three_epochs"),
    m("build_messages_on_loop", "s24-loop", W + "render_loader.py",
      "return await asyncio.to_thread(\n            _build_messages, ",
      "return _build_messages(\n            ", RS, "locked_productions"),
    m("reconcile_on_loop", "s24-loop", W + "render_store.py",
      "await asyncio.to_thread(reconciler.add_slice, inp, files)",
      "reconciler.add_slice(inp, files)", RS, "locked_productions"),
    # the blocking-I/O guard (§24.8): each category undetected is caught by its own loopblock test
    m("block_socket_not_detected", "s24-loop", LB,
      "        return bool(sock.gettimeout() != 0)  # type: ignore[attr-defined]",
      "        return False  # type: ignore[attr-defined]", ULB, "blocking_socket_call"),
    m("block_time_sleep_not_detected", "s24-loop", LB,
      "if cfg is not None and seconds and seconds > 0 and _on_loop():",
      "if cfg is not None and False and _on_loop():", ULB, "time_sleep"),
    m("block_raw_io_threshold_ignored", "s24-loop", LB,
      "if cfg is not None and _on_loop() and n >= cfg.threshold_bytes and blocks(args):",
      "if cfg is not None and _on_loop() and n >= (1 << 60) and blocks(args):", ULB, "raw_read_or_write"),
    m("block_file_read_not_detected", "s24-loop", LB,
      "if nbytes >= cfg.threshold_bytes and _on_loop() and _current() is not None:",
      "if False and _on_loop() and _current() is not None:", ULB, "buffered_file_read"),
    # ------------------------------------------------------------------ M16 step 1: the report model
    m("clean_ignores_custody", "m16-model", RM, "is_clean and custody_ok and divergences == 0",
      "is_clean and divergences == 0", UM, "clean"),
    m("clean_ignores_divergences", "m16-model", RM, "custody_ok and divergences == 0 and retention",
      "custody_ok and retention", UM, "clean"),
    m("clean_ignores_retention_gaps", "m16-model", RM, " and retention_gaps == 0\n", "\n", RT,
      "retention_gap"),
    m("custody_failure_not_announced", "m16-model", RM,
      '        lines.append("CUSTODY VERIFICATION FAILED")', "        pass", UM, "banners"),
    m("archive_caveat_dropped", "m16-model", RM, "        lines.append(ARCHIVE_CAVEAT)\n", "", UM,
      "banners"),
    m("zero_rows_dropped", "m16-model", RM, "for v in (*order, *extra)]",
      "for v in (*order, *extra) if counts.get(v)]", UM, "enum_value"),
    m("unknown_printed_as_none", "m16-model", RM, "return UNKNOWN if value is None else value",
      "return value", UM, "unknowns"),
    m("severity_order_swapped", "m16-model", RM, '    "failed", "access_lost", "gap",',
      '    "access_lost", "failed", "gap",', UM, "capped"),
    m("database_value_stated", "m16-model", RM, "            stated[key] = cs[0]",
      "            stated[key] = ds[0] if ds else cs[0]", UM, "compare_units"),
    m("duplicate_events_unnoticed", "m16-model", RM, "        if len(cs) > 1:", "        if False:",
      UM, "compare_units"),
    m("error_truncation_ignored", "m16-model", RM, "return (kind, rest[:2000]) if sep",
      "return (kind, rest) if sep", UM, "truncations"),
    m("buckets_never_differ", "m16-model", RM,
      "        return [i for i in range(BUCKETS) if self.buckets[i] != other.buckets[i]]",
      "        return []", RT, "tampered_work_unit"),
    m("jsonl_digest_skips_lines", "m16-model", RM, "        self.sha.update(line)\n", "", RT,
      "states_the_oracle"),
    m("pause_resumer_lost", "m16-model", RM, "p.resumed_at, p.resumed_by = e.created_at, e.actor",
      "p.resumed_at, p.resumed_by = e.created_at, None", WR, "pause"),
    m("database_nlo_ignored", "m16-model", RM, "no_longer_observed=no_longer_observed,",
      "no_longer_observed=0,", RT, "no_longer_observed"),
    m("units_file_unordered", "m16-model", RL,
      '("file", False): _UNIT_COLUMNS + " ORDER BY w.conversation_id, w.day, w.unit_key LIMIT :n"',
      '("file", False): _UNIT_COLUMNS + " ORDER BY w.unit_key DESC LIMIT :n"', RT, "states_the_oracle"),
    m("divergent_unit_states_database", "m16-model", RL,
      "fact: m.UnitFact | None = stated.get(key)", "fact: m.UnitFact | None = None", RT,
      "tampered_work_unit"),
    m("render_externals_dropped", "m16-model", RL, 'render["externals"] = [', 'render["dropped"] = [',
      RR, "external_natives"),
    m("zone_not_recorded", "m16-model", W + "pipeline.py", '                "unit_day_zone": "UTC",\n',
      "", RT, "states_the_oracle"),
    m("blind_spots_not_recorded", "m16-model", W + "pipeline.py",
      '"blind_spots": None if access.blind_spots is None else list(access.blind_spots),',
      '"blind_spots": None,', AE),
    m("connection_blind_spots_not_stored", "m16-model", "apps/api/src/edisc_api/routes/connections.py",
      '"blind": list(info.blind_spots), "cfg"', '"blind": None, "cfg"', AE),
    # ------------------------------------------------------------------ M16 step 4: the report stream
    m("report_begin_unfenced", "m16-stream", RW, '            if cur.status != "snapshotted":\n                return str(cur.status)\n            await close_episodes',
      "            if False:\n                return str(cur.status)\n            await close_episodes", RLC, "two_executors"),
    m("report_file_record_unchecked", "m16-stream", RW,
      "                if file_record(dict(existing._mapping)) != record:", "                if False:",
      RLC, "does_not_reproduce"),
    m("report_generated_unchecked", "m16-stream", RW,
      "            if recorded != stored or cur.files_done != len(stored):", "            if False:",
      RLC, "file_list_unlike"),
    m("report_seal_recorded_twice", "m16-stream", RW,
      '" WHERE id = :i AND seal_storage_key IS NULL RETURNING id"', '" WHERE id = :i RETURNING id"',
      RLC, "two_executors"),
    m("report_unsealed_job_accepted", "m16-stream", RW,
      "        if job.sealed_at is None or job.seal_storage_key is None:", "        if False:", RLC,
      "not_sealed"),
    m("report_runtime_unchecked", "m16-stream", RW,
      "        self._check_identity(row)\n        async with tenant_tx(self.sessions, tenant_id) as s:\n            job = (",
      "        async with tenant_tx(self.sessions, tenant_id) as s:\n            job = (", RLC,
      "another_runtime"),
    m("report_missing_never_closed", "m16-stream", RW,
      '                    await close_episodes(s, cur.job_id, "report_completed", kind="report_missing")\n',
      "                    pass\n", RWF, "ensure_job_reports"),
    m("report_divergence_unalerted", "m16-stream", RW,
      "                await self._divergence_alert(s, cur, len(built.divergences))", "                pass",
      RLC, "divergence"),
    m("report_sealing_stuck_never_flagged", "m16-stream", RW,
      "            if failures >= self.settings.render_seal_stuck_attempts:", "            if False:",
      ROP, "sealing_stuck"),
    m("report_unroutable_never_flagged", "m16-stream", W + "report_ops.py",
      "            elif await open_episode(", "            elif False and await open_episode(", ROP,
      "unroutable"),
    m("report_evidence_stats_unfixed", "m16-stream", RL,
      " AND report_id IS NULL AND kind NOT IN ('production', 'report')", "", RLC, "identical_identity"),
    m("report_anchor_unowned", "m16-stream", C + "log.py", "    if report is not None:",
      "    if False:", ROP, "files_and_anchors"),
    m("report_retention_route", "m16-stream", C + "retention_extension.py",
      '"   JOIN collection_jobs j ON j.matter_id = m.id JOIN reports rp ON rp.job_id = j.id"',
      '"   JOIN collection_jobs j ON j.matter_id = m.id JOIN reports rp ON false"', ROP,
      "files_and_anchors"),
    # ------------------------------------------------------------------ M16 step 2: the report HTML
    m("html_no_escape", "m16-html", RH, '        .replace("<", "&lt;")\n', '        .replace("<", "<")\n',
      UH, "escaping"),
    m("html_no_bdi", "m16-html", RH,
      "    return f\"<bdi>{reveal('' if value is None else str(value))}</bdi>\"",
      "    return reveal('' if value is None else str(value))", UH, "escaping"),
    m("html_bidi_not_replaced", "m16-html", RH,
      "            out.append(marker(cp))  # replaced: a marker after it would itself be reordered",
      "            out.append(ch + marker(cp))  # replaced: a marker after it would itself be reordered",
      UH, "bidi_controls_are_replaced"),
    m("html_invisible_not_marked", "m16-html", RH,
      "            out.append(marker(cp))  # kept, with the marker after it",
      "            pass  # kept, with the marker after it", UH, "bidi_controls_are_replaced"),
    m("html_banner_always_clean", "m16-html", RH,
      '    cls = "banner clean" if clean else "banner notclean"', '    cls = "banner clean"',
      UH, "clean_banner_and_the_not_clean"),
    m("html_cap_no_remainder", "m16-html", RH, "    if more and more_in:\n", "    if False and more_in:\n",
      UH, "capped_list_names_the_remainder"),
    m("html_counts_hide_zeros", "m16-html", RH,
      'return table(["value", "count"], [[user(r["value"]), num(r["count"])] for r in rows])',
      'return table(["value", "count"], [[user(r["value"]), num(r["count"])] for r in rows if r["count"]])',
      UH, "every_enum_value"),
    m("html_csp_without_style_hash", "m16-html", RH,
      "    f\"default-src 'none'; style-src '{STYLE_CSP_HASH}'; img-src 'none'; base-uri 'none'; \"",
      "    f\"default-src 'none'; style-src 'self'; img-src 'none'; base-uri 'none'; \"",
      UH, "only_allowed_elements"),
    # ------------------------------------------------------------------ fixes before M16 step 3
    m("marker_as_text", "m16-pre3", RH, '    return f\'<span class="cp">U+{cp:04X}</span>\'',
      '    return f"[U+{cp:04X}]"', UH, "marker_is_an_element"),
    m("marker_forgeable", "m16-pre3", RH, '            out.append(escape("".join(run)))\n            run.clear()\n            out.append(marker(cp))  # replaced',
      '            out.append("".join(run))\n            run.clear()\n            out.append(marker(cp))  # replaced',
      UH, "marker_is_an_element"),
    m("html_c1_kept", "m16-pre3", RH, "        or 0x7F <= cp <= 0x9F  # DEL and C1\n",
      "        or 0x7F == cp  # DEL and C1\n", UH, "invalid_in_html"),
    m("html_c0_kept", "m16-pre3", RH,
      "        (cp < 0x20 and cp not in _HTML_WHITESPACE_CONTROLS)  # NUL and C0 (form feed included)",
      "        (cp == 0 and cp not in _HTML_WHITESPACE_CONTROLS)  # NUL and C0 (form feed included)",
      UH, "invalid_in_html"),
    m("html_surrogate_kept", "m16-pre3", RH, "        or 0xD800 <= cp <= 0xDFFF  # a lone surrogate\n",
      "", UH, "surrogate"),
    m("html_nonchar_kept", "m16-pre3", RH, "        or (cp & 0xFFFE) == 0xFFFE  # U+xFFFE / U+xFFFF of every plane\n",
      "", UH, "invalid_in_html"),
    m("conversations_file_skipped", "m16-pre3", RL, "        if units.conversations.count > m.CAP:",
      "        if False:", RCC, "cap_boundary"),
    m("conversations_cap_off_by_one", "m16-pre3", RL, "        if units.conversations.count > m.CAP:",
      "        if units.conversations.count >= m.CAP:", RCC, "cap_boundary"),
    m("conversations_named_without_file", "m16-pre3", RM,
      "    if capped.total > capped.cap and file is None:", "    if False:", UMU,
      "file_is_required"),
    m("conversation_worst_wrong", "m16-pre3", RM,
      "    return min(statuses, key=lambda s: (_RANK.get(s, len(SEVERITY)), s), default=None)",
      "    return max(statuses, key=lambda s: (_RANK.get(s, len(SEVERITY)), s), default=None)",
      UMU, "conversation"),
    m("conversations_rows_on_loop", "m16-pre3", RL,
      "                take(fold.add(row), out)\n            return b\"\".join(out)\n\n        async for rows in self._unit_pages(\"file\"):\n            yield await asyncio.to_thread(page, rows)",
      "                take(fold.add(row), out)\n            return b\"\".join(out)\n\n        async for rows in self._unit_pages(\"file\"):\n            yield page(rows)",
      RCC, "cap_boundary and 1001"),
    m("report_bytes_change_without_bump", "m16-pre3", RH, '        el("title", "Collection report"),',
      '        el("title", "Collection report."),', UG, "golden_bytes"),
    m("shard_before_deselection", "m16-pre3", ICF, "    @pytest.hookimpl(trylast=True)\n",
      "    @pytest.hookimpl(tryfirst=True)\n", USH, "verify_uses_the_collected_set"),
    m("verify_from_durations", "m16-pre3", SH, "    collected = collect(target)\n",
      "    collected = sorted(load_durations())\n", USH, "verify_uses_the_collected_set"),
    # ------------------------------------------------------------------ M16 step 3: the PDF
    m("pdf_concurrency_ignored", "m16-pdf", RP,
      "        sem = per_loop[concurrency] = asyncio.Semaphore(concurrency)",
      "        sem = per_loop[concurrency] = asyncio.Semaphore(concurrency + 1)", UPR, "one_at_a_time"),
    m("pdf_overrun_not_killed", "m16-pdf", RP,
      "        except TimeoutError as exc:\n            await _kill(proc)\n",
      "        except TimeoutError as exc:\n", UPR, "overrunning"),
    m("pdf_cancel_not_killed", "m16-pdf", RP,
      "        except BaseException:  # cancelled (attempt timed out or worker stopping), oversize, ...\n            await _kill(proc)\n",
      "        except BaseException:  # cancelled (attempt timed out or worker stopping), oversize, ...\n",
      UPR, "cancelled_attempt"),
    m("pdf_truncated_accepted", "m16-pdf", RP, "    if not complete_pdf(out):\n",
      "    if not out:\n", UPR, "retryable_pdf_error"),
    m("pdf_nonzero_exit_accepted", "m16-pdf", RP, "    if code != 0:\n", "    if code not in (0, 2):\n",
      UPR, "retryable_pdf_error"),
    m("pdf_child_env_leaks", "m16-pdf", RP,
      "    env = {k: environ[k] for k in _CHILD_ENV if k in environ}",
      "    env = dict(environ)", UPR, "no_credentials"),
    m("pdf_output_unbounded", "m16-pdf", RP, "            if size > max_pdf_bytes:\n",
      "            if False:\n", UPR, "beyond_the_bound"),
    m("toolchain_without_zlib", "m16-pdf", VER, '"libpng16-16", "libbrotli1", "zlib1g", "libexpat1"',
      '"libpng16-16", "libbrotli1", "libexpat1"', UTM, "zlib"),
    m("toolchain_with_installers", "m16-pdf", VER,
      'INSTALLERS = frozenset({"pip", "setuptools", "wheel"})', "INSTALLERS = frozenset()",
      UTM, "installers"),
    m("icc_check_off", "m16-pdf", VER, "    if bundled != vendored:\n", "    if False:\n", UTM, "icc"),
    m("no_toolchain_in_production", "m16-pdf", VER, "        if env_is_ephemeral:\n",
      "        if True:\n", UTM, "only_where_allowed"),
    m("glyph_coverage_off", "m16-pdf", RH,
      "        not covered(cp) and not ch.isspace()", "        False and not ch.isspace()", UH,
      "no_vendored_font"),
    m("pdf_dates_missing", "m16-pdf", RH, "        *dates,\n", "", UH, "seal_dates"),
    m("page_identity_missing", "m16-pdf", RH, "        _page_identity(doc),\n", "", UH, "seal_dates"),
    # ------------------------------------------------------------------ fixes before M16 step 5
    m("source_json_replaces_surrogates", "m16-pre5", CAN,
      '            parts.append(b"\\\\u%04x" % cp)', '            parts.append("\\ufffd".encode())',
      ULS, "escapes or round_trips"),
    m("source_json_accepts_joined_pair", "m16-pre5", CAN, "            if high and cp >= 0xDC00:",
      "            if False:", ULS, "high_then_low"),
    m("normalizer_strict_hash", "m16-pre5", NSL,
      "from edisc_core.canonical import canonical_source_hash as canonical_hash",
      "from edisc_core.canonical import canonical_hash", UNS, "hashed_losslessly"),
    m("derived_stored_raw", "m16-pre5", NMO,
      '            object.__setattr__(self, "derived", {**valid, FIELD: found})', "            pass",
      UNS, "hashed_losslessly"),
    m("hints_stored_raw", "m16-pre5", NSL, "        hints = _stored_hints(exact_hints)",
      "        hints = exact_hints", UNS, "hints_are_stored"),
    m("identity_with_surrogate_stored", "m16-pre5", NSL, "    if has_lone(subject):", "    if False:",
      UNS, "identity_holding"),
    m("loader_states_replacement", "m16-pre5", RLO, "    found = derived.get(LONE_SURROGATES)",
      "    found = None", CORPUS, "lone_surrogates"),
    m("rsmf_raw_surrogates", "m16-pre5", R + "render.py", "    valid, found = make_valid(obj)",
      "    valid, found = obj, {}", URE, "lone_surrogates"),
    m("verifier_strict_fragment_hash", "m16-pre5", PKG,
      "from edisc_core.canonical import canonical_source_hash\n",
      "from edisc_core.canonical import canonical_hash as canonical_source_hash\n", CORPUS,
      "lone_surrogates"),
    m("failure_text_raw", "m16-pre5", PIPE, "        chain_error, exact = recorded_text(error, 2000)",
      "        chain_error, exact = error[:2000], {}", RLS, "lone_surrogate"),
    m("report_row_raw", "m16-pre5", RM, "    valid, found = make_valid(record)",
      "    valid, found = dict(record), {}", UH, "code_units"),
    m("report_json_raw", "m16-pre5", RM, "    valid, found = make_valid(doc)",
      "    valid, found = dict(doc), {}", UH, "valid_in_report_json"),
    m("report_count_missing", "m16-pre5", RM, "        self.lone_surrogates += found\n", "", UH,
      "code_units"),
    m("report_html_shows_replacement", "m16-pre5", RH, "    doc = exact_document(doc)\n", "", UH,
      "valid_in_report_json"),
    m("report_states_db_error", "m16-pre5", RM, "            if fact.error_exact is not None:",
      "            if False:", RLS, "lone_surrogate"),
    m("export_key_with_surrogate", "m16-pre5", LAY,
      "    return value if isinstance(value, str) and not has_lone(value) else None",
      "    return value if isinstance(value, str) else None", ULA, "not_storable"),
    m("glyph_set_not_in_identity", "m16-pre5", RW,
      '        "glyph_set": glyph_set_id(),', '        "glyph_set": "0" * 64,', UGI, "new_identity"),
    m("glyph_set_not_in_queue", "m16-pre5", CON, '        f".g{glyph_set[:12]}"', '        ""', UGI,
      "new_identity"),
    m("glyph_set_ignores_coverage", "m16-pre5", RH,
      '    return hashlib.sha256(lines.encode("ascii")).hexdigest()',
      '    return hashlib.sha256(b"").hexdigest()', UGI, "new_identity"),
    m("report_created_without_glyph_set", "m16-pre5", RW, '            "gs": ident["glyph_set"],',
      '            "gs": "unrecorded",', RLC, "stored_recorded_and_sealed"),
    m("pdf_from_unchecked_html", "m16-pre5", RW,
      '        if hashlib.sha256(body).hexdigest() != html["sha256"] or len(body) != html["size"]:',
      "        if False:", RPS, "altered"),
    m("api_needs_a_dev_only_package", "m16-pre5", "apps/api/src/edisc_api/main.py",
      "from edisc_api.app import create_app\n",
      "import html5lib  # noqa: F401\nfrom edisc_api.app import create_app\n", PROD),
    m("worker_needs_a_dev_only_package", "m16-pre5", W + "__main__.py", "def main() -> None:\n",
      "import hypothesis  # noqa: F401,E402\n\n\ndef main() -> None:\n", PROD),
    # image-only breaks: run inside the report-test image (scripts/mutation/run.py --kind image)
    m("pdf_bytes_change", "m16-pre5", RP, "        uncompressed_pdf=False,\n",
      "        uncompressed_pdf=False,\n        custom_metadata=True,\n", IPG, "goldens"),
    m("child_without_rlimit", "m16-pre5", RP,
      "    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))\n", "", IPD,
      "memory_limit"),
    m("fonts_conf_scans_system_fonts", "m16-pre5", FONTS_CONF, "  <dir>/opt/edisc/fonts</dir>\n",
      "  <dir>/opt/edisc/fonts</dir>\n  <dir>/usr/share/fonts</dir>\n", IFI, "vendored_dir"),
    m("child_env_without_fontconfig", "m16-pre5", RP, '_CHILD_ENV = ("PATH", "FONTCONFIG_FILE", ',
      '_CHILD_ENV = ("PATH", ', IPG, "goldens"),
    # ------------------------------------------------------------------ M16 step 5: API, packages
    m("job_package_unsealed_served", "m16-api", JP, "        if job.sealed_at is None:",
      "        if False:", AJP, "not_sealed"),
    m("job_package_wrong_permission", "m16-api", JP,
      '        await authorize(s, caller, P.EXPORT_READ, Scope("matter", job.matter_id))',
      '        await authorize(s, caller, P.JOB_READ, Scope("matter", job.matter_id))', AJP,
      "only_matter_managers"),
    m("job_package_bytes_before_anchor", "m16-api", JP,
      "    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)\n", "",
      AJP, "first_byte"),
    m("job_package_not_deterministic", "m16-api", JEX,
      '        "sealed_at": format_utc(job.sealed_at) if job.sealed_at else None,',
      '        "sealed_at": str(uuid.uuid4()),', AJP, "byte_identical"),
    m("job_package_length_wrong", "m16-api", JP, '            "content-length": str(plan.zip_size),',
      '            "content-length": str(plan.zip_size - 1),', AJP, "byte_identical"),
    m("job_package_abort_unaudited", "m16-api", JP,
      "            await _aborted(res, caller, rid, plan, base, exc)\n", "", AJP, "aborts"),
    m("collector_cannot_read_reports", "m16-api", AUTHZ,
      '    "collector": frozenset({P.JOB_START, P.JOB_CANCEL, P.REPORT_READ, *_READ_MATTER}),',
      '    "collector": frozenset({P.JOB_START, P.JOB_CANCEL, *_READ_MATTER}),', ARP,
      "every_role_reads"),
    m("reviewer_may_request_reports", "m16-api", AUTHZ,
      '    "reviewer": frozenset({P.EVIDENCE_READ, P.REPORT_READ, *_READ_MATTER}),',
      '    "reviewer": frozenset({P.EVIDENCE_READ, P.REPORT_READ, P.REPORT_CREATE, *_READ_MATTER}),',
      ARP, "every_role_reads"),
    m("report_reason_not_recorded", "m16-api", REP, "request_reason=body.reason,",
      "request_reason=None,", ARP, "who_and_why"),
    m("report_file_bytes_before_anchor", "m16-api", REP,
      "    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)\n"
      "    writer = EvidenceWriter", "    writer = EvidenceWriter", ARP, "first_byte"),
    m("report_package_bytes_before_anchor", "m16-api", REP,
      "    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)\n\n"
      "    async def body()", "\n    async def body()", ARP, "first_byte"),
    m("report_file_not_rehashed", "m16-api", REP,
      "        if (digest.hexdigest(), size) != (f.sha256, f.size_bytes):", "        if False:", ARP,
      "stored_bytes_differ"),
    m("report_package_abort_unaudited", "m16-api", REP,
      '            await _aborted(res, caller, rid, row.job_id, "report_package_aborted",',
      '            None and _aborted(res, caller, rid, row.job_id, "report_package_aborted",', ARP,
      "package_object_that_differs"),
    m("evidence_endpoint_serves_reports", "m16-api", JOBS,
      'row.kind in ("production", "report")', 'row.kind in ("production",)', ARP,
      "never_served_by_the_evidence"),
    m("verifier_skips_recomputation", "m16-api", RPK,
      '    report.errors += [f"recomputed from the job chain: {d}" for d in done.differences]',
      "    pass", ARP, "other_facts"),
    m("verifier_accepts_unlisted_files", "m16-api", RPK,
      '            report.errors.append(f"{extra}: not part of this package")', "            pass",
      ARP, "unlisted"),
    m("verifier_zip_job_package_unread", "m16-api", PS,
      "    return ZipSource(path) if is_zip(path) else DirectorySource(path)",
      "    return DirectorySource(path)", ARP, "recomputes_from_the_job_package"),
    # ------------------------------------------------------------------ fixes before step 6 (§28)
    # fix 1: hostile export values never drop data
    m("export_name_refused", "m16-pre6", LAY,
      '        exact["/name"] = utf16_units(name)\n',
      '        exact["/name"] = utf16_units(name)\n        return None\n', ULA, "kept_exactly"),
    m("export_hostile_id_dropped", "m16-pre6", LAY, "        refused = REFUSED_ID",
      "        return None", ULA, "stand_in"),
    m("export_hostile_team_guessed", "m16-pre6", LAY,
      "            refused = refused or REFUSED_TEAM\n", "", ULA, "stand_in"),
    m("export_hostile_workspace_used", "m16-pre6", EXW,
      "            scan.workspace_unkeyable = scan.workspace is None",
      "            scan.workspace_unkeyable = False", AEC, "lone_surrogates_refuse"),
    m("export_exact_name_not_stored", "m16-pre6", EXW,
      '"exact": json.dumps(rec.exact) if rec.exact else None,', '"exact": None,', ACE,
      "hostile_channel_name"),
    m("export_hostile_ts_indexed", "m16-pre6", EXW, "                if storable_key(ts) is None:",
      "                if False:", ACE, "message_keys"),
    m("refused_conversation_collected", "m16-pre6", CONX,
      "        if refused is not None:\n            raise UncollectableUnitError",
      "        if False:\n            raise UncollectableUnitError", ACE, "hostile_channel_id"),
    m("uncollectable_unit_not_recorded", "m16-pre6", PIPE,
      "            await self._record_uncollectable(tenant_id, job_id, unit_key, exc.reason)\n", "",
      ACE, "hostile_channel_id"),
    m("hostile_ts_not_selected", "m16-pre6", CONX, "            if hostile_ts\n            else None",
      "            if False\n            else None", ACE, "message_keys"),
    m("subjects_ignore_select", "m16-pre6", NSL,
      '        if select is not None and raw.get("ts") not in select:\n            continue\n        mid',
      "        mid", ACE, "message_keys"),
    m("batch_gaps_dropped", "m16-pre6", PIPE, '"gr": sorted(set(batch.gaps)),', '"gr": [],', ACE,
      "message_keys"),
    m("gap_reasons_ignored_in_recon", "m16-pre6", PIPE, "                and not row.gap_reasons\n",
      "", ACE, "message_keys"),
    m("render_export_name_not_restored", "m16-pre6", RLO,
      '            name = restore({"name": name}, {"/name": meta.exact["/name"]})["name"]',
      "            pass", ACE, "hostile_channel_name"),
    # fix 2: every unit fact in the chain, recomputed byte for byte
    m("unit_event_without_detail", "m16-pre6", PIPE,
      '                        else {}\n                    ),\n                    **detail,\n',
      '                        else {}\n                    ),\n', RUF, "every_unit_event"),
    m("unit_event_format_not_marked", "m16-pre6", PIPE,
      '                "unit_event_format": UNIT_EVENT_FORMAT,\n', "", RUF, "every_unit_event"),
    m("unsettled_units_not_recorded", "m16-pre6", PIPE,
      "for at in range(0, len(unsettled), UNSETTLED_BATCH):",
      "for at in range(0, 0, UNSETTLED_BATCH):", RUF, "cancelled"),
    m("unit_row_ignores_chain_detail", "m16-pre6", RM, "            if d is not None\n",
      "            if False\n", RUF, "every_unit_event"),
    m("older_job_details_trusted", "m16-pre6", RM, "    if detailed:\n        return facts",
      "    if True:\n        return facts", URC, "before_the_format"),
    m("recompute_skips_units_file", "m16-pre6", RC, "    if rebuilt != units_jsonl:",
      "    if False:", URC, "names_what_differs"),
    m("recompute_skips_sections", "m16-pre6", RC,
      "        if canonical_json(mine[section]) != canonical_json(stated[section]):",
      "        if False:", URC, "names_what_differs"),
    # fix 3: the job package verifier strict, zips read in place
    m("job_package_accepts_unlisted", "m16-pre6", PKG,
      '            report.errors.append(f"{extra}: not part of this package")', "            pass",
      TP, "manifest_does_not_list"),
    m("job_package_tolerates_in_zip", "m16-pre6", PKG,
      "        tolerate = tolerate_os_metadata and not isinstance(source, ZipSource)",
      "        tolerate = tolerate_os_metadata", TP, "only_in_a_directory"),
    m("job_package_copied_out", "m16-pre6", PKG,
      "    source = open_source(path)\n    try:\n        tolerate",
      "    import tempfile\n\n    tempfile.mkdtemp()\n    source = open_source(path)\n    try:\n"
      "        tolerate", TP, "in_place"),
    m("embedded_archive_not_read_in_zip", "m16-pre6", PA,
      "        return lambda: EntrySource(source.stored_range(name))",
      "        raise TypeError(name)", AEP, "inside_a_zip"),
    # ------------------------------------------------------------------ M16 step 6: the preview
    m("preview_not_anchored", "m16-preview", PRV,
      "    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)\n",
      "", APV, "first_byte"),
    m("preview_without_csp", "m16-preview", PRV, '    "content-security-policy": CSP_HEADER,\n',
      "", APV, "first_byte"),
    m("preview_refusal_not_audited", "m16-preview", PRV,
      "        await _refused(res, caller, rid, job_id, request, exc.code, exc.detail,"
      " integrity=False)\n", "", APV, "refusals"),
    m("preview_outside_scope_served", "m16-preview", PVW,
      "        if not any(r.date_from < end and r.date_to > start for r in ranges):",
      "        if False:", APV, "refusals"),
    m("preview_bidi_kept", "m16-preview", PVH,
      "    return cp in BIDI_CONTROLS or invalid_in_html(cp)", "    return invalid_in_html(cp)",
      UPV, "safe"),
    m("preview_text_not_escaped", "m16-preview", PVH,
      "    return f\"<bdi>{reveal('' if value is None else str(value))}</bdi>\"",
      "    return f\"<bdi>{'' if value is None else str(value)}</bdi>\"", UPV, "safe"),
    m("preview_any_handle_linked", "m16-preview", PVH, "            if _UUID.match(f.handle):",
      "            if True:", UPV, "safe"),
    m("preview_drops_a_page", "m16-preview", PVH,
      "    pages = split_parts(primaries, context_root, cap) if primaries else [[]]",
      "    pages = split_parts(primaries, context_root, cap)[:1] if primaries else [[]]", UPV,
      "n_plus_one"),
    m("day_slice_reads_the_whole_conversation", "m16-preview", RLO,
      "            index = await self._linked(s, rows, conversation_id)",
      "            index = await self._index(conversation_id)", APV, "bounded"),
    m("day_slice_roots_not_looked_up", "m16-preview", RLO, "            if absent:",
      "            if False:", APV, "every_page"),
    m("preview_render_on_event_loop", "m16-preview", PVW,
      "        rendered = await asyncio.to_thread(\n"
      "            preview_html.render_preview, inp, page, preview_html.PREVIEW_PAGE_EVENTS\n"
      "        )", "        rendered = preview_html.render_preview(inp, page,"
      " preview_html.PREVIEW_PAGE_EVENTS)", APV, "first_byte"),
    # ------------------------------------------------------------------ fixes before step 7 (§30)
    m("scope_order_by_locale", "m16-pre7", M35, '    ("collection_scopes", "external_id"),\n', "",
      DCO),
    m("package_items_by_locale", "m16-pre7", M35, '    ("items", "idempotency_key"),\n', "", CCO,
      "slack_three_epochs"),
    m("report_units_by_locale", "m16-pre7", M33,
      '    ALTER COLUMN conversation_id TYPE text COLLATE "C",\n',
      "    ALTER COLUMN conversation_id TYPE text,\n", CCO, "slack_three_epochs"),
    m("gap_reasons_by_locale", "m16-pre7", PIPE,
      'array_agg(DISTINCT r COLLATE "C" ORDER BY r COLLATE "C")', "array_agg(DISTINCT r ORDER BY r)",
      DCO),
    m("derivation_order_by_locale", "m16-pre7", NST, ' x.normalizer_version COLLATE "C" DESC',
      " x.normalizer_version DESC", DCO),
    m("verdict_not_checked", "m16-pre7", RC, "    if out.stated_clean and not out.chain_clean:",
      "    if False:", URV, "forged"),
    m("verdict_ignores_units", "m16-pre7", RC, "        elif fact.recon_status in UNCLEAN_RECON:",
      "        elif False:", URV, "consistent_looking"),
    m("verdict_ignores_banner", "m16-pre7", RC,
      '    return doc.get("job", {}).get("clean") is True or CLEAN_BANNER in (doc.get("banner") or [])',
      '    return doc.get("job", {}).get("clean") is True', URV, "for_a_chain_that_is_not"),
    m("verdict_accepts_archive", "m16-pre7", RC, "fold.final_status != JobStatus.COMPLETED.value:",
      "fold.final_status not in (JobStatus.COMPLETED.value, JobStatus.COMPLETED_AGAINST_ARCHIVE.value):",
      URV, "every_reason"),
    m("verdict_forged_passes_end_to_end", "m16-pre7", RC,
      "    if out.stated_clean and not out.chain_clean:", "    if False:", ARP, "forged"),
    m("stated_reasons_not_printed", "m16-pre7", CLI,
      '                    sys.stdout.write(f"    report: {reason}\\n")', "                    pass",
      ARP, "not_clean_for_a_clean_chain"),
    m("normalizers_not_in_job_started", "m16-pre7", PIPE,
      '                "normalizer_versions": [NORMALIZER_VERSION],\n', "", ARP, "normalizer"),
    m("normalizer_not_in_batches", "m16-pre7", PIPE,
      "            normalizer_version=NORMALIZER_VERSION,\n", "", ARP, "normalizer"),
    m("report_normalizers_from_derivations", "m16-pre7", RL,
      "            if chain.normalizer_versions is not None", "            if False", ARP,
      "normalizer"),
    m("normalizers_not_recomputed", "m16-pre7", RC, '"actors", "versions", "counts",',
      '"actors", "counts",', URV, "normalizer_versions_are_stated"),
    # ------------------------------------------------------------------ the export in the report (§32)
    m("export_findings_not_recorded", "m16-export", PIPE,
      '            k: v for k, v in findings.items() if k not in ("blind_spots", "tier_warnings")\n',
      '            k: v for k, v in findings.items() if False\n', ACE, "export_public_only"),
    m("export_plan_not_recorded", "m16-export", PIPE, '        "declared_plan": row.declared_plan,',
      '        "declared_plan": None,', ACE, "export_public_only"),
    m("export_blind_spots_not_recorded", "m16-export", PIPE,
      '        "blind_spots": list(findings.get("blind_spots", [])),', '        "blind_spots": [],',
      ACE, "export_grid"),
    m("export_unknown_printed_as_null", "m16-export", RM,
      "    return {k: recorded.get(k, UNKNOWN) for k in EXPORT_FIELDS}",
      "    return {k: recorded.get(k) for k in EXPORT_FIELDS}", UEX, "unknown"),
    m("export_access_not_recomputed", "m16-export", RC, '("job", "scopes", "access", "pauses",',
      '("job", "scopes", "pauses",', ACE, "export_public_only"),
    m("export_section_not_in_html", "m16-export", RH,
      '        + _export(doc["access"].get("export"), doc["access"].get("source")),\n',
      "        ,\n", UEX, "cannot_contain"),
    m("export_blind_spots_not_in_html", "m16-export", RH,
      '        _statements(export.get("blind_spots")),', "        _statements([]),", UEX,
      "cannot_contain"),
    m("export_findings_by_locale", "m16-export", M35, '    ("export_entries", "folder"),\n', "", CCO,
      "export_case"),
    # ------------------------------------------------------------------ no error echoes a value (D11)
    m("echo_422_default_handler", "m17-d11", SE,
      "    app.add_exception_handler(RequestValidationError, _validation_error)\n", "", NOE,
      "any_route_and_field"),
    m("echo_422_default_handler_unit", "m17-d11", SE,
      "    app.add_exception_handler(RequestValidationError, _validation_error)\n", "", USE,
      "wrong_body"),
    m("echo_422_input_kept", "m17-d11", SE,
      '                "msg": _message(e),\n',
      '                "msg": _message(e),\n                "input": e.get("input"),\n', USE,
      "never_carries"),
    m("echo_422_client_keys_kept", "m17-d11", SE,
      "part in names or (i == 0 and part in LOCATIONS)", "True", USE, "client_chosen"),
    m("echo_422_messages_kept", "m17-d11", SE,
      "    if kind in SCHEMA_MESSAGES or kind.startswith(OWN_PREFIX):", "    if True:", USE,
      "wrong_body"),
    m("echo_500_unhandled_not_caught", "m17-d11", SE,
      "    app.add_middleware(SafeErrors, segments=lambda: segments)\n", "", USE, "fixed_500"),
    m("echo_500_message_logged", "m17-d11", SE,
      "        request_id=request_id, response_started=started,",
      "        request_id=request_id, response_started=started, message=str(exc),", USE,
      "fixed_500"),
    m("echo_stream_error_swallowed", "m17-d11", SE, "            if started:\n                raise",
      "            if started:\n                return", USE, "first_byte"),
    m("echo_raw_path_in_log_context", "m17-d11", APPM, "        bind_context(request_id=rid, path=path)",
      "        bind_context(request_id=rid, path=request.url.path)", NOE, "any_route_and_field"),
    m("echo_uvicorn_loggers_not_routed", "m17-d11", "apps/api/src/edisc_api/main.py",
      "route_server_logs()  #", "pass  #", PROD),
    m("echo_uvicorn_access_log_on", "m17-d11", SE, "    access.disabled = True\n", "", USE,
      "uvicorn_access"),
    m("echo_sql_parameters", "m17-d11", SESS, "        hide_parameters=True,\n", "", DBS),
    m("echo_recorded_text_not_redacted", "m17-d11", ERRS,
      "RecordedText = Annotated[str, AfterValidator(redact_text)]", "RecordedText = str", USE,
      "recorded_worker_texts"),
    m("echo_unknown_source", "m17-d11", "apps/api/src/edisc_api/routes/connections.py",
      '        raise unprocessable("unknown source")', '        raise unprocessable(f"unknown source {body.source}")',
      NOE, "handler_refusals"),
    m("echo_unknown_role", "m17-d11", "apps/api/src/edisc_api/routes/admin.py",
      'PydanticCustomError("edisc_unknown_role", "unknown role")',
      'PydanticCustomError("edisc_unknown_role", f"unknown role {self.role}")', NOE,
      "handler_refusals"),
    m("echo_render_zone", "m17-d11", API,
      '        raise unprocessable("unknown time zone (IANA names of the pinned tzdata)") from exc',
      "        raise unprocessable(str(exc)) from exc", NOE, "handler_refusals"),
    m("echo_preview_refusal_detail", "m17-d11", PRV, "exc.code, REFUSED_DETAIL[exc.code]) from exc",
      "exc.code, exc.detail) from exc", APV, "refusals_are_audited"),
    # ------------------------------------------------------------------ log assertions see the pipeline
    Mutation("log_credential_before_registration", "m17-logs", CONN, (
        ("from edisc_core.ids import new_id\n",
         "from edisc_core.ids import new_id\nfrom edisc_core.logs import get_logger\n"),
        ("def _tokens(c: Credentials, version: int = 0) -> TokenSet:\n",
         "def _tokens(c: Credentials, version: int = 0) -> TokenSet:\n"
         "    get_logger(__name__).info(\"credentials\", note=c.access_token.get_secret_value())\n"),
    ), ACO, "no_credential_is_ever_returned"),
    m("log_access_line_query", "m17-logs", APPM, 'route=getattr(route, "path", None),',
      "route=str(request.url),", USE, "raw_bodies"),
    m("log_validation_errors_raw", "m17-logs", SE,
      "        errors = exc.errors() if isinstance(exc, RequestValidationError) else ()\n",
      "        errors = exc.errors() if isinstance(exc, RequestValidationError) else ()\n"
      "        log.info(\"validation\", errors=repr(errors))\n", USE, "wrong_body"),
    m("log_auth_failure_host", "m17-logs", APPM,
      'log.info("authentication failed", reason=str(exc), path=request_path(request))',
      'log.info("authentication failed", reason=str(exc), path=request.headers.get("host"))', NOE,
      "bearer_token_or_host"),
    Mutation("log_refusal_value", "m17-logs", CONN, (
        ("from edisc_core.ids import new_id\n",
         "from edisc_core.ids import new_id\nfrom edisc_core.logs import get_logger\n"),
        ('        raise unprocessable("unknown source")',
         '        get_logger(__name__).info("refused", source=body.source)\n'
         '        raise unprocessable("unknown source")'),
    ), NOE, "handler_refusals"),
    m("log_opened_secret_not_registered", "m17-logs", ENV,
      "        value = raw.decode(\"utf-8\")\n        if len(value) >= MIN_SECRET_LENGTH:\n            register_secret(value)\n",
      "        value = raw.decode(\"utf-8\")\n", UKMS, "redacted_from_logs"),
    m("log_sealed_secret_not_registered", "m17-logs", ENV,
      "            register_secret(value)  # anything we ever seal", "            pass  # anything we ever seal",
      UKMS, "redacted_from_logs"),
    # ------------------------------------------------------------------ browser sessions (M17 step 2)
    m("sess_fixation_planted_id_kept", "m17-sessions", AUR,
      "    session_value, session_row = new_token(), new_id()",
      "    session_value, session_row = request.cookies.get(SESSION_COOKIE) or new_token(), new_id()",
      SES, "planted_session_id"),
    m("sess_prior_not_rotated", "m17-sessions", AUR, "        if prior is not None:\n            rotated = (",
      "        if False:\n            rotated = (", SES, "revokes_the_session_the_browser_held"),
    m("sess_idle_expiry_unchecked", "m17-sessions", AUTH,
      '" AND s.idle_expires_at > now() AND s.absolute_expires_at > now()"',
      '" AND s.absolute_expires_at > now()"', SES, "expired_session_is_refused and idle"),
    # the query's absolute-expiry term is redundant by construction (idle <= absolute: the CHECK, and
    # the activity write's LEAST): breaking it alone is unobservable, so the CHECK is what is broken
    m("sess_idle_past_absolute_allowed", "m17-sessions", M36,
      "    CONSTRAINT ck_sessions_expiry CHECK (idle_expires_at <= absolute_expires_at\n        AND absolute_expires_at > created_at),",
      "    CONSTRAINT ck_sessions_expiry CHECK (absolute_expires_at > created_at),", DSG,
      "without_a_reason"),
    m("sess_revocation_unchecked", "m17-sessions", AUTH, '" WHERE s.id_hash = :h AND s.revoked_at IS NULL"',
      '" WHERE s.id_hash = :h"', SES, "logout_revokes"),
    m("sess_inactive_principal_accepted", "m17-sessions", AUTH, "            if row is None or not row.active:",
      "            if row is None:", SES, "inactive_principal_is_refused_even"),
    m("sess_activity_write_unthrottled", "m17-sessions", AUTH,
      " AND last_seen_at < now() - interval '60 seconds'\"", "\"", SES, "at_most_once_a_minute"),
    m("sess_idle_past_absolute", "m17-sessions", AUTH,
      "LEAST(now() + make_interval(secs => :idle), absolute_expires_at)", "now() + make_interval(secs => :idle)",
      SES, "at_most_once_a_minute"),
    m("sess_cookie_not_httponly", "m17-sessions", SESP, "Path=/; Secure; HttpOnly; SameSite", "Path=/; Secure; SameSite",
      SES, "strict_host_only_session"),
    m("sess_cookie_not_secure", "m17-sessions", SESP, "Path=/; Secure; HttpOnly; SameSite", "Path=/; HttpOnly; SameSite",
      SES, "strict_host_only_session"),
    m("sess_cookie_domain_added", "m17-sessions", SESP, "Max-Age={max_age}; Path=/;",
      "Max-Age={max_age}; Domain=edisc.localhost; Path=/;", SES, "strict_host_only_session"),
    m("sess_cookie_lax", "m17-sessions", AUR, '                   same_site="Strict"),',
      '                   same_site="Lax"),', SES, "strict_host_only_session"),
    m("sess_cookie_lax_in_browsers", "m17-sessions", AUR, '                   same_site="Strict"),',
      '                   same_site="Lax"),', SESB, "chromium"),
    m("sess_cookie_and_bearer_accepted", "m17-sessions", APPM,
      "    if session_id is not None and authorization is not None:  # before either is evaluated",
      "    if False:  # before either is evaluated", SES, "cookie_and_bearer_together"),
    m("csrf_token_not_compared", "m17-sessions", SESP,
      "        return hmac.compare_digest(expected.encode(), mac.encode())", "        return True", SES,
      "need_the_token_and_the_tenant_origin"),
    m("csrf_origin_unchecked", "m17-sessions", APPM,
      "        if request_origin(request) != tenant_origin(res.settings, host):",
      "        if False:", SES, "need_the_token_and_the_tenant_origin"),
    m("csrf_required_from_bearer", "m17-sessions", APPM, "    if session_id is None:\n        return await",
      "    if session_id is None and request.method in SAFE_METHODS:\n        return await", SES,
      "need_the_token_and_the_tenant_origin"),
    m("fetch_metadata_unchecked", "m17-sessions", APPM,
      '    if site is not None and site not in ("same-origin", "none"):',
      "    if False:", SES, "another_site_or_a_sibling_subdomain"),
    m("fetch_metadata_same_site_allowed", "m17-sessions", APPM,
      '    if site is not None and site not in ("same-origin", "none"):',
      '    if site is not None and site not in ("same-origin", "same-site", "none"):', SES,
      "another_site_or_a_sibling_subdomain"),
    m("cookie_responses_cacheable", "m17-sessions", APPM,
      '            response.headers["cache-control"] = "no-store"\n            vary',
      "            vary", SES, "strict_host_only_session"),
    m("login_state_reusable", "m17-sessions", AUR, '" WHERE state_hash = :h AND consumed_at IS NULL"',
      '" WHERE state_hash = :h"', SES, "state_is_single_use"),
    m("login_handle_unchecked", "m17-sessions", AUR,
      "    if handle is None or not well_formed(handle) or token_hash(handle) != attempt.handle_hash:",
      "    if handle is None:", SES, "another_browsers_login_cookie"),
    m("login_expiry_unchecked", "m17-sessions", AUR, "    if not attempt.fresh:", "    if False:", SES,
      "expired_attempt"),
    m("login_nonce_unchecked", "m17-sessions", OIDC,
      '        if not isinstance(claims["nonce"], str) or claims["nonce"] != nonce:',
      '        if not isinstance(claims["nonce"], str):', SES, "does_not_check_out"),
    m("return_to_double_slash", "m17-sessions", SESP, 'candidate.startswith(("//", "/\\\\"))',
      'candidate.startswith("/\\\\")', USP, "refuses_everything"),
    m("return_to_not_decoded", "m17-sessions", SESP, "    for candidate in (value, urllib.parse.unquote(value)):",
      "    for candidate in (value,):", SES, "not_a_same_origin_path"),
    m("return_to_controls_kept", "m17-sessions", SESP, '        if "\\\\" in candidate or _CONTROL.search(candidate):',
      '        if "\\\\" in candidate:', USP, "refuses_everything"),
    m("return_to_unbounded", "m17-sessions", SESP, "    if len(value.encode(\"utf-8\", \"surrogatepass\")) > MAX_RETURN_TO_BYTES:",
      "    if False:", SES, "not_a_same_origin_path"),
    m("reauth_other_principal", "m17-sessions", AUR, "        or prior.principal_id != principal.id\n", "", SES,
      "other_principal"),
    m("reauth_stale_auth_time", "m17-sessions", AUR, "        or claims.auth_time <= a.started_at\n", "", SES,
      "stale_auth_time"),
    m("callback_303_not_bounce", "m17-sessions", AUR,
      "    response = HTMLResponse(bounce_page(target), status_code=status, headers=BOUNCE_HEADERS)",
      "    response = RedirectResponse(target, status_code=303)", SESB, "chromium"),
    m("deactivation_keeps_sessions", "m17-sessions", ADM,
      '        ended = await revoke_sessions(s, principal_id, caller.actor, "principal_deactivated")',
      "        ended: list[uuid.UUID] = []", SES, "deactivated_principal"),
    m("recent_sign_in_not_required", "m17-sessions", AUTH, "    at = caller.authenticated_at\n",
      "    at = caller.authenticated_at\n    return at.isoformat() if at else ''\n", SES,
      "every_sensitive_action"),
    m("login_starts_unthrottled", "m17-sessions", AUR, "    await _count_start(request, res)\n", "", SES,
      "starts_are_capped"),
    m("session_revocation_not_final", "m17-sessions", M36,
      "    IF OLD.revoked_at IS NOT NULL THEN\n        RAISE EXCEPTION 'sessions: a revoked session is final'",
      "    IF false THEN\n        RAISE EXCEPTION 'sessions: a revoked session is final'", DSG, "revoked_session_is_final"),
    # ------------------------------------------------------------------ pool churn (ADR 0015 §24.13)
    m("pool_overflow_back", "s24-pool", SESS, "        max_overflow=0,\n",
      "        max_overflow=pool_size,\n", IPOOL),
    m("pool_never_marked_warm", "s24-pool", SESS, "    _warmed.add(pool)\n", "    pass\n", IPOOL,
      "new_slot"),
    m("pool_churn_not_reported", "s24-pool", SESS, "            loopblock.report_product(\n",
      "            (lambda *a: None)(\n", IPOOL, "new_slot"),
    m("pool_reconnect_is_churn", "s24-pool", SESS,
      'first, info["edisc_connected"] = not info.get("edisc_connected"), True',
      'first, info["edisc_connected"] = True, True', IPOOL, "reconnect"),
    m("guard_idle_never_counts", "s24-pool", LG,
      "            return self.cpu_ms is not None and self.busy  # measured, never assumed\n",
      "            return False\n", ULG, "measured_busy"),
    m("guard_idle_assumed_busy", "s24-pool", LG,
      "            return self.cpu_ms is not None and self.busy  # measured, never assumed\n",
      "            return self.busy\n", ULG, "measured_busy"),
    m("guard_wrapper_hides_send", "s24-pool", LG,
      "        return frame.f_back  # the blocking-I/O guard's wrapper around the transport's call\n",
      "        return frame\n", ULG, "wrapped_by_the_blocking"),
    # ------------------------------------------------------------------ host-only session cookie
    Mutation("session_cookie_domain_wide", "m17-host-cookie", SESP, (
        ('SESSION_COOKIE = "__Host-edisc_session"', 'SESSION_COOKIE = "edisc_session"'),
        ('    return f"{name}={value}; Max-Age={max_age}; Path=/; Secure; HttpOnly; SameSite={same_site}"',
         '    domain = "; Domain=edisc-browser.test" if name == SESSION_COOKIE else ""\n'
         '    return f"{name}={value}; Max-Age={max_age}; Path=/{domain}; Secure; HttpOnly; SameSite={same_site}"'),
    ), SESB, "another_subdomain and chromium"),
    m("same_site_fetch_allowed", "m17-host-cookie", APPM,
      'if site is not None and site not in ("same-origin", "none"):',
      'if site is not None and site not in ("same-origin", "none", "same-site"):', SESB,
      "another_subdomain and webkit"),
]  # fmt: skip
