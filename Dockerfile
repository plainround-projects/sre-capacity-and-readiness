# One image for every process in the stack: capacity-instance, capacity-ledger and capacity-loadtest are all entry points.
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

RUN useradd --system --uid 10001 --create-home app && mkdir -p /app/data /app/logs && chown -R app /app/data /app/logs
USER app
EXPOSE 8080 8081 9100 9101
CMD ["capacity-instance"]
