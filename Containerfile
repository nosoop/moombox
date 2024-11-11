FROM docker.io/library/python:3.11-bookworm

# Install the project into `/app`
WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends --assume-yes ffmpeg ca-certificates git

# Add source code and set up an editable install
ADD . /app
RUN --mount=type=cache,target=/root/.cache/pip \
    python3.11 -m venv /app/.venv && /app/.venv/bin/pip install -e "/app"

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# moombox has an environment variable backdoor to override the instance path
ENV MOOMBOX_INSTANCE_PATH="/data/config"

ENTRYPOINT []

# Ensure that hypercorn is doing work in our data directory; moombox does workdir-relative paths
WORKDIR "/data"
CMD ["hypercorn", "moombox.app:create_app()", "-w", "1", "-b", "0.0.0.0:5000"]
