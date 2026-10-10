# The collection report worker image (ADR 0018 §5.2-§5.4, §6; ADR 0017). linux/amd64 ONLY: PDF bytes
# are promised only inside this pinned image. Built from the repository root:
#
#   docker buildx build --platform linux/amd64 -f deploy/report-image/Dockerfile \
#       --target report -t edisc-report:dev --load .
#
# Pinned inputs: the base image by digest; Debian packages by a snapshot.debian.org timestamp (a
# rebuild gets the same packages, zlib included: it makes the compressed PDF streams, §5.6); Python
# packages by uv.lock; fonts by commit URL + SHA-256 (fonts.lock), instanced with a pinned fontTools.
# Every one of them is part of the PDF toolchain id (`python -m edisc_worker.versions`), except the
# installers, which take no part in rendering.
#
# Targets: `report` (production: the worker, the PDF child, the fonts) and `report-test` (the same
# layers plus test tooling in a SEPARATE venv, /opt/edisc/testenv, so pytest/pypdf never enter the
# worker's interpreter and its toolchain id; the PDF child of a test runs /opt/edisc/venv/bin/python).

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af

FROM --platform=linux/amd64 ${PYTHON_IMAGE} AS base
ARG SNAPSHOT=20261001T000000Z
RUN rm -f /etc/apt/sources.list.d/debian.sources \
 && printf '%s\n' \
    "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${SNAPSHOT} bookworm main" \
    "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${SNAPSHOT} bookworm-updates main" \
    "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${SNAPSHOT} bookworm-security main" \
    > /etc/apt/sources.list \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 libfontconfig1 fontconfig \
    libfreetype6 libfribidi0 \
 && rm -rf /var/lib/apt/lists/* \
 && rm -rf /usr/share/fonts/* /etc/fonts/conf.d/*

# ---------------------------------------------------------------- fonts (build-only stage)
FROM base AS fonts
COPY deploy/report-image/fonts.lock deploy/report-image/fetch_fonts.py /build/
# fontTools instances the variable fonts: pinned, because the instanced bytes depend on it (their
# SHA-256 is in the toolchain id, so a different fontTools would show up there anyway)
RUN pip install --no-cache-dir fonttools==4.66.1 \
 && python /build/fetch_fonts.py /build/fonts.lock /opt/edisc

# ---------------------------------------------------------------- venvs (build-only stage)
FROM base AS venv
RUN pip install --no-cache-dir uv==0.10.11
ENV UV_PYTHON=/usr/local/bin/python3.12 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=0
WORKDIR /src
COPY pyproject.toml uv.lock .python-version ./
COPY packages packages
COPY workers workers
COPY apps/api apps/api
# the worker's environment: production dependencies only, workspace packages installed (not editable)
RUN UV_PROJECT_ENVIRONMENT=/opt/edisc/venv uv sync --frozen --no-dev --package edisc-worker --no-editable
# the test environment (report-test only): everything plus the dev group
RUN UV_PROJECT_ENVIRONMENT=/opt/edisc/testenv uv sync --frozen --all-packages --no-editable

# ---------------------------------------------------------------- production
FROM base AS report
COPY --from=fonts /opt/edisc/fonts /opt/edisc/fonts
COPY deploy/report-image/fonts.conf /opt/edisc/fonts.conf
RUN FONTCONFIG_FILE=/opt/edisc/fonts.conf fc-cache -f \
 && chmod -R a-w /opt/edisc/fonts /opt/edisc/fc-cache
COPY --from=venv /opt/edisc/venv /opt/edisc/venv
ENV FONTCONFIG_FILE=/opt/edisc/fonts.conf \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/edisc/venv/bin:$PATH \
    EDISC_REPORT_PDF_PYTHON=/opt/edisc/venv/bin/python \
    EDISC_REPORT_FONT_DIR=/opt/edisc/fonts
ENTRYPOINT ["python", "-m", "edisc_worker"]
CMD ["--reports"]

# ---------------------------------------------------------------- admission / tests
FROM report AS report-test
COPY --from=fonts /opt/edisc/leak /opt/edisc/leak
COPY --from=venv /opt/edisc/testenv /opt/edisc/testenv
WORKDIR /src
COPY pyproject.toml uv.lock .python-version ./
COPY tests tests
COPY scripts scripts
COPY deploy deploy
ENV PATH=/opt/edisc/testenv/bin:$PATH
ENTRYPOINT []
CMD ["python", "-m", "pytest", "tests/report_image", "-q", "-p", "no:randomly"]
