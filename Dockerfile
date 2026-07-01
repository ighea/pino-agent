FROM python:3.12-slim

ARG UID=1000
ARG GID=1000
ARG USERNAME=dev

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    nano \
    sudo \
    unzip \
    vim \
    xz-utils \
    build-essential \
    nodejs \
    npm \
    sqlite3 \
    tmux \
    libolm-dev \
  && rm -rf /var/lib/apt/lists/*

RUN if id -u ubuntu >/dev/null 2>&1; then userdel -r ubuntu || true; fi \
 && groupadd dev \
 && useradd -m -g dev -s /bin/bash dev \
 && echo "dev ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dev \
 && chmod 0440 /etc/sudoers.d/dev \
 && chown -R dev:dev /home/dev

WORKDIR /code

# Build the project venv from requirements.txt so it's baked into the image.
# docker-compose overlays a named volume on top of /code/.venv (see docker-compose.yml)
# so this survives the `.:/code` bind mount instead of being hidden by it.
COPY requirements.txt ./
RUN python3 -m venv /code/.venv \
 && /code/.venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
 && /code/.venv/bin/pip install --no-cache-dir -r requirements.txt \
 && chown -R dev:dev /code

USER dev

# Use the venv-installed binaries first
ENV PATH="/code/.venv/bin:${PATH}"

# Add venv PATH and Ollama environment variables to .bashrc for interactive shells
RUN echo '' >> ~/.bashrc && \
    echo '# Add venv binaries to PATH' >> ~/.bashrc && \
    echo 'export PATH="/code/.venv/bin:${PATH}"' >> ~/.bashrc

CMD ["/bin/bash"]
