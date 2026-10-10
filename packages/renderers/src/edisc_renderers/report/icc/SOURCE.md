# sRGB2014.icc (ADR 0018 §5.6, amendment 5)

- File: `sRGB2014.icc`, 3,024 bytes, SHA-256
  `384b832de3412066743b52a75ee906b6fb9fb8d9e09e936fc2c43223815c6e0a`, ICC v2, embedded profile id
  `3d0eb2deae9397be9b6726ce8c0a43ce` (equal to the MD5 recomputed over the profile: self-consistent).
- Source: the copy bundled in the WeasyPrint 70.0 wheel (`weasyprint/pdf/sRGB2014.icc`), extracted
  2026-10-10 from `weasyprint-70.0-py3-none-any.whl` (PyPI). WeasyPrint embeds ITS bundled copy as the
  PDF/A output intent, so the report PDF child refuses to render unless that copy is byte-equal to
  this file (`edisc_worker.report_pdf`).
- Licence: `LICENSE` (ICC's general licensing terms for ICC-owned profiles: unaltered copies may be
  embedded without restriction).
- **Not yet verified against the official color.org file** (color.org served HTML to scripted
  downloads during spike S1). Required before production: docs/BACKLOG.md. Record the result here.
