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
- ``s24-loop``: the event-loop guard (timing and the OFF_LOOP registry in ``tests/conftest.py``),
  every CPU step moved off the loop, the render thread writing nothing (ADR 0015 §24).
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
DC = "packages/connectors/dummy/src/edisc_connector_dummy/connector.py"
ULG = "tests/unit/core/test_loopguard.py"
UJ = "tests/unit/core/test_jsonstream.py"
UE = "tests/unit/renderers/test_eml.py"
UT = "tests/unit/renderers/test_render_thread_writes_nothing.py"
UDE = "tests/unit/dummy/test_epochs_failures_policy.py"
CORPUS = "tests/integration/corpus/test_corpus.py"
PL = "tests/integration/pipeline/test_pipeline.py"
CL = "tests/integration/custody/test_custody_log.py"
RS = "tests/integration/renders/test_render_store.py"
NS = "packages/normalizer/src/edisc_normalizer/store.py"


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
    m("tolerate_too_much", "s19-review", C + "render_package.py",
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
      '"   JOIN evidence_objects e ON e.render_id = r.id),"',
      "\"   JOIN evidence_objects e ON e.render_id = r.id AND e.storage_key NOT LIKE '%/natives/%'),\"",
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
      "        async with self._content_lock(key):\n            return await self._write_production_locked(",
      "        async with asyncio.timeout(None):\n            return await self._write_production_locked(",
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
    m("guard_scram_counted", "s24-loop", LG, "    if _connection_auth(stack):\n", "    if False:\n",
      ULG, "scram"),
    m("guard_follows_sync_drivers", "s24-loop", LG,
      "        if not f.f_code.co_flags & (_ASYNC | inspect.CO_GENERATOR):\n            break\n", "",
      ULG, "synchronously"),
    m("jsonstream_no_yield", "s24-loop", "packages/core/src/edisc_core/jsonstream.py",
      "        await asyncio.sleep(0)\n", "", UJ, "turn"),
    m("zipwriter_no_yield", "s24-loop", C + "zipwriter.py",
      "        await asyncio.sleep(0)\n", "", UZ, "ready_members"),
    m("entry_reader_no_yield", "s24-loop", C + "archive.py",
      "        await asyncio.sleep(0)\n        if inflater is None:", "        if inflater is None:",
      ULT, "turn_per_chunk"),
    m("sync_driver_refuses_turns", "s24-loop", C + "package_source.py",
      "        while steps.send(None) is None:\n            pass\n",
      "        if steps.send(None) is None:\n            pass\n", ULT, "drivers"),
    m("rsmf_driver_refuses_turns", "s24-loop", C + "rsmf_check.py",
      "        while steps.send(None) is None:\n            pass\n",
      "        if steps.send(None) is None:\n            pass\n", ULT, "drivers"),
    m("envelope_no_yield", "s24-loop", R + "eml.py", "        await asyncio.sleep(0)\n", "", UE, "turn"),
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
      "subjects = await asyncio.to_thread(message_page_subjects, batch.body, ctx=ctx)",
      "subjects = message_page_subjects(batch.body, ctx=ctx)", PL, "three_epochs"),
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
]  # fmt: skip
