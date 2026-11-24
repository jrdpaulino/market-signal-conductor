FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY msc ./msc
# stdlib only: nothing to pip install
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
ARG MSC_COMMIT=unknown
ENV MSC_DB=/data/msc.sqlite PORT=8080 MSC_COMMIT=$MSC_COMMIT
EXPOSE 8080
CMD ["python", "-m", "msc", "serve"]
