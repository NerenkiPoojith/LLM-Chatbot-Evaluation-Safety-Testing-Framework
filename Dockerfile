# Container image for the chatbot (API + web pages), used by Render (render.yaml) and Hugging Face Spaces.
# Any Docker host works too:  docker build -t appliance-help . && docker run -p 7860:7860 appliance-help
FROM python:3.12-slim

# OpenCV, used by the OCR engine, needs these system libraries
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs the container as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    PORT=7860 \
    # Public demo: the free offline answerer, so no API key lives on the server and
    # visitors can't spend anyone's API credits
    LLM_PROVIDER=mock \
    ANTHROPIC_API_KEY= \
    # Host names the app answers to (it refuses others to block DNS rebinding)
    ALLOWED_HOSTS=*.onrender.com,*.hf.space,localhost,127.0.0.1 \
    # Smaller limits than local use, so one visitor can't keep the shared CPU busy
    MAX_UPLOAD_MB=10 \
    OCR_MAX_PAGES=20

WORKDIR /home/user/app

# Runtime dependencies only (test and PDF-building tools are left out)
COPY --chown=user requirements.txt .
RUN grep -vE '^(pytest|pytest-html|reportlab)([<>=~ ]|$)' requirements.txt > /tmp/requirements-runtime.txt \
 && pip install --no-cache-dir --user -r /tmp/requirements-runtime.txt

COPY --chown=user . .

# Download the embedding and OCR models now, so the app starts without fetching them
RUN python -c "\
from app.config import get_settings; \
from app.embeddings import get_embeddings; \
from app.formats import english_model; \
s = get_settings(); \
get_embeddings(s.embedding_model, str(s.embedding_cache_dir)).embed_query('warm up'); \
assert english_model(), 'OCR model download failed'"

EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips='*'"]
