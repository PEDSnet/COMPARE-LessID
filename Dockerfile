# lessid — Python de-identification layer
#
# SAS 9.4 is NOT included in this image.  It must be licensed to the host
# machine and bind-mounted in.  Set sas_bin in config/lessid.toml to point
# to the mounted SAS binary (e.g. /host_sas/SASFoundation/9.4/bin/sas_u8).
#
# Build:
#   podman build -t lessid .
#
# Run (minimal):
#   podman run --rm \
#     -v /path/to/config/lessid.toml:/app/config/lessid.toml:ro \
#     -v /data/cpt:/data/cpt:ro \
#     -v /data/output:/data/output \
#     -v /data/lookup:/data/lookup \
#     -v /data/work:/data/work \
#     -v /usr/local/SAS:/host_sas:ro \
#     lessid run --yes

FROM python:3.11-slim

LABEL org.opencontainers.image.title="lessid"
LABEL org.opencontainers.image.description="CDM de-identification pipeline (Python layer)"

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first so layer is cached independently of code
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline sources
COPY src/ ./src/
COPY sas/ ./sas/
COPY config/lessid.example.toml ./config/

# config/lessid.toml is gitignored and must be volume-mounted at runtime.
# The example is included so users can see the expected structure inside
# the container.

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "src/pipeline.py"]
CMD ["--help"]
