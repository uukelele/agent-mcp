FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

RUN useradd --create-home --uid 10001 mcp \
    && mkdir -p /workspace \
    && chown mcp:mcp /workspace
USER mcp
WORKDIR /workspace

EXPOSE 8765
ENTRYPOINT ["agent-mcp"]
CMD ["--host", "0.0.0.0", "--port", "8765", "--root", "/workspace"]
