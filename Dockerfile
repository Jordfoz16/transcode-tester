# VMAF transcode comparison - requires ffmpeg with libvmaf
FROM linuxserver/ffmpeg

# Install Python and pip
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application
COPY vmaf_compare.py .

# Override entrypoint: run our script; docker run args become script args
ENTRYPOINT ["python3", "vmaf_compare.py"]
