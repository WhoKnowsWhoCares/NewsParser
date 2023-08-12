FROM python:3.10-slim as base

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1

# Set the working directory to /app
RUN mkdir /app
WORKDIR /app

FROM base as builder

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
        pip install -r requirements.txt

COPY app.py .
COPY main.py .
COPY .env .
COPY src/ ./src/
COPY templates/ ./templates/
# COPY newsparser.session /app/
RUN mkdir /app/logs

CMD ["python", "main.py"]
