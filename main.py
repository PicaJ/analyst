"""
analyst CLI 入口 — 闭环 Agent 模式

用法:
  # 闭环 Agent 分析 (推荐)
  python main.py run --entity "比亚迪" --days 90
  python main.py run --keywords "芯片,制裁" --days 60
  python main.py run --auto --days 30

  # 仅构建线索链 (不需要 LLM)
  python main.py chain timeline --entity "比亚迪" --days 90
  python main.py chain sector --keywords "新能源,补贴"
  python main.py chain anomaly --days 30
  python main.py chain cross --days 60

  # 生命周期管理
  python main.py status                    # 查看 Harness 状态
  python main.py resume <run_id>           # 恢复中断的运行
  python main.py runs                      # 列出所有运行记录
"""

import asyncio
import sys

import click
from loguru import logger

from analyst.config import load_config
from analyst.logging_config import setup_logging
from analyst.chain_builder import ChainBuilder
from analyst.harness import Harness
from analyst.state import StateStore


@click.group()
@click.option("--config", "-c", default=None, help="配置文件路径 (YAML)")
@click.pass_context
def cli(ctx, config):
    """analyst — 财经新闻闭环分析 Agent"""
    ctx.ensure_object(dict)
    cfg = load_config(config)
    ctx.obj["config"] = cfg

    # 初始化日志
    setup_logging(
        level=cfg.log_level,
        log_dir=cfg.log_dir if cfg.log_dir else None,
        retention=cfg.log_retention_days,
        error_retention=cfg.log_error_retention_days,
        max_size_gb=cfg.log_max_size_gb,
        cleanup_size_gb=cfg.log_cleanup_size_gb,
    )


# ========== 闭环 Agent 分析 (主入口) ==========

@cli.command()
@click.option("--entity", "-e", default=None, help="聚焦实体")
@click.option("--keywords", "-k", default=None, help="关键词 (逗号分隔)")
@click.option("--auto", is_flag=True, help="自动模式: 自动选择热门实体分析")
@click.option("--days", default=90, help="回溯天数")
@click.option("--max-iter", default=3, help="最大迭代次数")
@click.option("--no-report", is_flag=True, help="不生成报告文件")
@click.pass_context
def run(ctx, entity, keywords, auto, days, max_iter, no_report):
    """闭环 Agent 分析 (Plan → Execute → Evaluate → Refine)"""
    cfg = ctx.obj["config"]

    if not entity and not keywords and not auto:
        click.echo("请指定 --entity, --keywords, 或 --auto")
        sys.exit(1)

    kw_list = [k.strip() for k in keywords.split(",")] if keywords else []

    async def _run():
        harness = Harness(cfg)
        result = await harness.run_analysis(
            entity=entity,
            keywords=kw_list,
            days=days,
            max_iterations=max_iter,
            output_report=not no_report,
        )
        _print_run_result(result)

    asyncio.run(_run())


# ========== 线索链构建 (不需要 LLM) ==========

@cli.group()
@click.pass_context
def chain(ctx):
    """线索链构建 (不需要 LLM)"""
    pass


@chain.command("timeline")
@click.option("--entity", "-e", required=True, help="实体名称")
@click.option("--type", "entity_type", type=click.Choice(["company", "sector", "person"]),
              default="company")
@click.option("--days", default=90)
@click.pass_context
def chain_timeline(ctx, entity, entity_type, days):
    cfg = ctx.obj["config"]

    async def _run():
        builder = ChainBuilder(cfg)
        chains = await builder.build_timeline_chain(entity, entity_type, days)
        _print_chains(chains)
    asyncio.run(_run())


@chain.command("sector")
@click.option("--keywords", "-k", required=True, help="关键词 (逗号分隔)")
@click.option("--days", default=90)
@click.pass_context
def chain_sector(ctx, keywords, days):
    cfg = ctx.obj["config"]
    kw_list = [k.strip() for k in keywords.split(",")]

    async def _run():
        builder = ChainBuilder(cfg)
        chains = await builder.build_sector_propagation_chain(kw_list, days)
        _print_chains(chains)
    asyncio.run(_run())


@chain.command("anomaly")
@click.option("--days", default=30)
@click.pass_context
def chain_anomaly(ctx, days):
    cfg = ctx.obj["config"]

    async def _run():
        builder = ChainBuilder(cfg)
        chains = await builder.build_anomaly_chains(days)
        _print_chains(chains)
    asyncio.run(_run())


@chain.command("cross")
@click.option("--days", default=60)
@click.pass_context
def chain_cross(ctx, days):
    cfg = ctx.obj["config"]

    async def _run():
        builder = ChainBuilder(cfg)
        chains = await builder.build_entity_cross_chains(days)
        _print_chains(chains)
    asyncio.run(_run())


# ========== 生命周期管理 ==========

@cli.command()
@click.pass_context
def status(ctx):
    """查看 Harness 状态和指标"""
    cfg = ctx.obj["config"]
    harness = Harness(cfg)
    s = harness.status()

    click.echo("=== Harness 状态 ===")
    m = s["metrics"]
    click.echo(f"总运行: {m['runs_total']}  成功: {m['runs_success']}  失败: {m['runs_failed']}")
    click.echo(f"成功率: {m['success_rate']:.1%}  平均质量: {m['avg_quality_score']:.3f}")
    click.echo(f"LLM 调用: {m['total_llm_calls']}  总迭代: {m['total_iterations']}")

    cb = s["circuit_breaker"]
    state = "OPEN (熔断中)" if cb["is_open"] else "CLOSED (正常)"
    click.echo(f"熔断器: {state}  (连续失败: {cb['consecutive_failures']}/{cb['threshold']})")

    if s["recent_runs"]:
        click.echo("\n最近运行:")
        for r in s["recent_runs"][:5]:
            click.echo(f"  {r['run_id']}  state={r['state']}  "
                       f"score={r['score']:.3f}  iter={r['iteration']}  "
                       f"updated={r['updated_at'][:19]}")


@cli.command()
@click.argument("run_id")
@click.pass_context
def resume(ctx, run_id):
    """恢复中断的运行"""
    cfg = ctx.obj["config"]

    async def _run():
        harness = Harness(cfg)
        result = await harness.resume(run_id)
        if result:
            _print_run_result(result)
        else:
            click.echo(f"运行 {run_id} 不存在")
    asyncio.run(_run())


@cli.command()
@click.pass_context
def runs(ctx):
    """列出所有运行记录"""
    cfg = ctx.obj["config"]
    store = StateStore(str(cfg.data_dir / "state"))
    all_runs = store.list_runs()

    if not all_runs:
        click.echo("暂无运行记录")
        return

    click.echo(f"{'run_id':<14} {'state':<10} {'score':>6} {'iter':>5} {'errors':>7} {'updated'}")
    click.echo("-" * 70)
    for r in all_runs:
        click.echo(f"{r['run_id']:<14} {r['state']:<10} {r['score']:>6.3f} "
                   f"{r['iteration']:>5} {r['errors_count']:>7} {r['updated_at'][:19]}")


# ========== 工具函数 ==========

def _print_run_result(ctx):
    """打印闭环运行结果"""
    click.echo(f"\n{'='*50}")
    click.echo(f"运行 ID: {ctx.run_id}")
    click.echo(f"状态: {ctx.state.value}")
    click.echo(f"迭代: {ctx.iteration}/{ctx.max_iterations}")
    click.echo(f"质量分数: {ctx.quality_score:.3f}")
    click.echo(f"LLM 调用: {ctx.total_llm_calls}")
    click.echo(f"耗时: {ctx.total_latency_ms:.0f}ms")

    if ctx.insights:
        click.echo(f"\n洞察 ({len(ctx.insights)} 条):")
        for i, ins in enumerate(ctx.insights, 1):
            thesis = ins.get("thesis", "?")[:60]
            conf = ins.get("confidence", 0)
            click.echo(f"  {i}. [{conf:.0%}] {thesis}")

    if ctx.evaluation:
        ev = ctx.evaluation
        click.echo(f"\n评估: {ev.get('overall_score', 0):.3f} "
                   f"(通过率: {ev.get('pass_rate', 0):.0%})")
        if ev.get("critique"):
            click.echo(f"批评: {ev['critique'][:100]}")

    if ctx.errors:
        click.echo(f"\n错误:")
        for e in ctx.errors[:5]:
            click.echo(f"  - {e}")

    if ctx.report_path:
        click.echo(f"\n报告: {ctx.report_path}")

    click.echo(f"{'='*50}")


def _print_chains(chains):
    """打印线索链摘要"""
    if not chains:
        click.echo("未发现线索链")
        return

    for i, c in enumerate(chains, 1):
        click.echo(f"\n{'='*50}")
        click.echo(f"线索链 #{i}: {c.theme}")
        click.echo(f"  类型: {c.chain_type} | 重要性: {c.significance:.2f}")
        click.echo(f"  时间跨度: {c.time_span} | 节点数: {c.node_count}")

        if c.hidden_signals:
            click.echo(f"  隐蔽信号:")
            for s in c.hidden_signals:
                click.echo(f"    - {s}")

        click.echo(f"  新闻:")
        for j, n in enumerate(c.nodes[:5], 1):
            time_str = n.publish_time[:16] if n.publish_time else "?"
            click.echo(f"    {j}. [{time_str}] [{n.source}] {n.title[:60]}")
        if c.node_count > 5:
            click.echo(f"    ... 还有 {c.node_count - 5} 条")

    click.echo(f"\n共 {len(chains)} 条线索链")


if __name__ == "__main__":
    cli()
