# Offline conformance image. Build with the cmpath checkout as context:
#   docker build -f integrations/deployment/Dockerfile -t cmpath-conformance .
FROM python:3.12-slim

WORKDIR /opt/cmpath
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY conformance ./conformance

RUN python -m pip install --no-cache-dir --no-deps .

ENV PYTHONUNBUFFERED=1
CMD ["python", "conformance/run.py"]

