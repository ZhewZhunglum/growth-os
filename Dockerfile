FROM python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime

ARG GIT_COMMIT_SHA

LABEL org.opencontainers.image.source="https://github.com/ZhewZhunglum/growth-os" \
      org.opencontainers.image.revision="${GIT_COMMIT_SHA}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GROWTH_OS_RELEASE_SHA="${GIT_COMMIT_SHA}"

WORKDIR /app

# A deployable image must identify one immutable full commit.  This validation
# also prevents a branch name, "latest", or an abbreviated SHA being recorded
# as deployment evidence.
RUN test "${#GIT_COMMIT_SHA}" -eq 40 \
    && case "${GIT_COMMIT_SHA}" in *[!0-9a-f]*) exit 1 ;; esac

RUN addgroup --system growthos && adduser --system --ingroup growthos growthos

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN mkdir -p /app/staticfiles /app/media && chown -R growthos:growthos /app

USER growthos

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=3)"

ENTRYPOINT ["sh", "/app/scripts/docker-entrypoint.sh"]
CMD ["gunicorn", "growth_os.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-"]

STOPSIGNAL SIGTERM
