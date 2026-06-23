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

  # Create a lightweight virtual environment and install aider into it
  RUN python3 -m venv /opt/venv \
   && /opt/venv/bin/python -m pip install --upgrade pip setuptools wheel flask requests \
   && /opt/venv/bin/pip install --no-cache-dir aider-chat

RUN if id -u ubuntu >/dev/null 2>&1; then userdel -r ubuntu || true; fi \
 && groupadd dev \
 && useradd -m -g dev -s /bin/bash dev \
 && echo "dev ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dev \
 && chmod 0440 /etc/sudoers.d/dev \
 && chown -R dev:dev /home/dev

USER dev
WORKDIR /code

# Use the venv-installed binaries first
ENV PATH="/opt/venv/bin:${PATH}"

# Add venv PATH and Ollama environment variables to .bashrc for interactive shells
RUN echo '' >> ~/.bashrc && \
    echo '# Add venv binaries to PATH' >> ~/.bashrc && \
    echo 'export PATH="/opt/venv/bin:${PATH}"' >> ~/.bashrc

CMD ["/bin/bash"]
