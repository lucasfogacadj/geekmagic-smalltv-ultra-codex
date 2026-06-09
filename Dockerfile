FROM python:3.12-slim AS icon-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

ARG CODEX_ICON_URL="https://persistent.oaistatic.com/codex/icon-gif.mp4"
RUN curl -fsSL "$CODEX_ICON_URL" -o codex-icon.mp4
RUN mkdir -p codex-icon-frames \
    && ffmpeg -hide_banner -loglevel error \
        -i codex-icon.mp4 \
        -vf "fps=8,scale=54:54:force_original_aspect_ratio=decrease,pad=54:54:(ow-iw)/2:(oh-ih)/2:color=black" \
        -frames:v 16 \
        codex-icon-frames/frame-%02d.png

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN mkdir -p /var/lib/codex/sessions /var/cache/smalltv-dashboard

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/smalltv_dashboard.py .
COPY --from=icon-builder /build/codex-icon-frames ./assets/codex-icon-frames

CMD ["python", "smalltv_dashboard.py"]
