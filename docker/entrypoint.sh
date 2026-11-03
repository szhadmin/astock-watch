#!/bin/sh
# astock-watch 容器入口：做几件安全检查，然后启动 Web 控制台
set -e
cd /app

echo "=============================================================="
echo "  astock-watch 启动中"
echo "  时区: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=============================================================="

# 1) 配置文件检查：config.json 必须是一个文件（含模型密钥）
if [ ! -f /app/config.json ]; then
  echo "[错误] 找不到 /app/config.json"
  echo "       请先在宿主机执行：  cp config.example.json config.json"
  echo "       然后填入 ai.api_key / base_url / model（以及可选 notify.email 邮箱）"
  echo "       再 docker compose up -d"
  exit 1
fi

# 2) 数据目录（首次运行会自动生成空账户与记忆文件）
mkdir -p /app/data /app/backups

# 3) 打印关键配置（密钥打码）
python - <<'PY' || true
import json
try:
    c = json.load(open("/app/config.json", encoding="utf-8-sig"))
    ai = c.get("ai") or {}
    key = ai.get("api_key") or ""
    masked = ("****" + key[-4:]) if key else "（未填写）"
    print("  模型: %s | %s | key=%s" % (ai.get("provider"), ai.get("model") or "-", masked))
    sch = ai.get("schedule") or {}
    print("  调度: %s | 决策每 %s 分钟 | 复盘 %s | 选股 %s | 计划 %s | 日报 %s"
          % ("开启" if sch.get("enabled") else "关闭", sch.get("decide_every_min"),
             sch.get("review_at"), sch.get("pick_at"), sch.get("plan_at"), sch.get("report_at")))
except Exception as e:
    print("  （配置解析失败：%s）" % e)
PY

# 4) 启动（默认监听 0.0.0.0，容器内必须这样；对外暴露请用 compose 端口映射控制）
exec python main.py serve --host 0.0.0.0 --port "${PORT:-8770}" "$@"
