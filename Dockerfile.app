ARG APP_NAME=newsparser_app
ARG PYTHON_VERSION=3.10.0
ARG POETRY_VERSION=1.5.1
ARG POETRY_HOME=/app/poetry

FROM python:3.10-slim as builder

ENV PYTHONDONTWRITEBYTECODE 1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1
ENV POETRY_VERSION=$POETRY_VERSION \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

RUN mkdir -p /app
WORKDIR /app

RUN pip install poetry
COPY poetry.lock pyproject.toml /app/
RUN poetry install --without dev,web

# COPY requirements.txt .
# RUN --mount=type=cache,target=/root/.cache/pip \
#         pip install --no-cache-dir -r requirements.txt


FROM python:3.10-slim as base

ENV PYTHONDONTWRITEBYTECODE 1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1

COPY --from=builder /app /app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

COPY main.py /app
COPY .env /app
COPY src/ /app/src/
RUN mkdir /app/logs

CMD ["python", "main.py"]
