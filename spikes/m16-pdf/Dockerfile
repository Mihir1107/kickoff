# M16 spike S1: WeasyPrint byte stability. linux/amd64 only (D3). Base pinned by digest, Debian
# packages pinned by a snapshot.debian.org timestamp, Python packages by exact version.
FROM --platform=linux/amd64 python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af

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

RUN pip install --no-cache-dir weasyprint==70.0 pypdf==6.19.0

COPY fonts.lock fetch_fonts.py fonts.conf /opt/edisc/
RUN python /opt/edisc/fetch_fonts.py /opt/edisc/fonts.lock \
 && FONTCONFIG_FILE=/opt/edisc/fonts.conf fc-cache -f \
 && chmod -R a-w /opt/edisc/fonts /opt/edisc/fc-cache

ENV FONTCONFIG_FILE=/opt/edisc/fonts.conf \
    PYTHONDONTWRITEBYTECODE=1
COPY spike.py /opt/edisc/spike.py
WORKDIR /work
ENTRYPOINT ["python", "/opt/edisc/spike.py"]
