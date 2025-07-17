FROM python:3.11-slim-bookworm

# Install the project into `/app`
WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends --assume-yes ffmpeg ca-certificates git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Add source code and set up an editable install
ADD . /app

RUN --mount=type=cache,target=/root/.cache/pip \
    python3.11 -m venv /app/.venv && /app/.venv/bin/pip install -e "/app"

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# moombox has an environment variable backdoor to override the instance path
ENV MOOMBOX_INSTANCE_PATH="/data/config"

# Ensure that hypercorn is doing work in our data directory; moombox does workdir-relative paths
WORKDIR /data

ENTRYPOINT [ "hypercorn", "moombox.app:create_app()", "--workers", "1" ]

CMD [ "--bind", "0.0.0.0:5000" ]
