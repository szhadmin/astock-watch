# ---- astock-watch 生产镜像 ----
# 核心代码零第三方依赖；akshare（财报/龙虎榜）为可选，构建时加 --build-arg WITH_AKSHARE=1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PORT=8770

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 可选依赖（akshare 会带 pandas/numpy，镜像会大不少）
COPY requirements-optional.txt /app/requirements-optional.txt
ARG WITH_AKSHARE=0
RUN if [ "$WITH_AKSHARE" = "1" ]; then \
      pip install --no-cache-dir -r /app/requirements-optional.txt ; \
    fi

COPY . /app

RUN chmod +x /app/docker/entrypoint.sh \
 && mkdir -p /app/data /app/backups

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/api/overview',timeout=5).status==200 else 1)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
