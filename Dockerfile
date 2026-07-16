# One image for both the Flask "brain" and the mitmproxy interceptor — they share
# deps and the same two source files, and the entrypoint runs both. python:3.12-slim
# is multi-arch, so this builds natively on x86-64 and arm64 (Apple Silicon, a Pi).
FROM python:3.12-slim

# Run as a non-root user. mitmproxy's CA (its trust anchor) lands under this user's
# home, which docker-compose mounts as a named volume so the CA — and therefore your
# device's trust — survives `docker compose down`.
RUN useradd --create-home --uid 10001 app
ENV MITM_CONFDIR=/home/app/.mitmproxy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py addon.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh \
    && mkdir -p "$MITM_CONFDIR" \
    && chown -R app:app /app "$MITM_CONFDIR"

USER app
# Explicit HTTP proxy port. Flask (:5000) stays inside the container — never exposed.
EXPOSE 8080
ENTRYPOINT ["./docker-entrypoint.sh"]
