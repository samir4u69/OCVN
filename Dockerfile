FROM python:3.10-slim

# Better logs and no pip cache
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    wget \
    tar \
    xz-utils \
    unzip \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install N_m3u8DL-RE (linux x64 build)
RUN wget https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.5.1-beta/N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz \
    && tar -xzf N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz \
    && find . -name "N_m3u8DL-RE" -type f -exec mv {} /usr/local/bin/N_m3u8DL-RE \; \
    && chmod +x /usr/local/bin/N_m3u8DL-RE \
    && rm -rf N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029*

# Optional but recommended: non-root user
RUN useradd -m appuser

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Drop privileges
USER appuser

# Start your bot
CMD ["python", "bot.py"]
