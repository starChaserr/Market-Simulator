import argparse
import json
import unittest
import datetime as dt
import os
import tempfile
from unittest import mock
from pathlib import Path

from example_agents import raider_core
import paper_trade.daily_supervisor as daily_supervisor
import paper_trade.run_live_match as run_live_match
from paper_trade.broker import PaperBroker, PaperConfig
from paper_trade.upstox_client import MarketQuote, QuoteLevel
from paper_trade.upstox_auth import token_file_is_fresh, write_access_token
from paper_trade.agent_registry import (
    active_agent_entries,
    default_registry,
    prune_losing_agents,
    register_challenger,
    save_agent_registry,
    update_agent_performance,
)
from paper_trade.auto_live_optimize import (
    build_gemini_prompt,
    resolve_symbols,
    seconds_until_market_live,
    summarize_results,
)
from paper_trade.daily_supervisor import (
    agent_source_path,
    build_supervisor_prompt,
    catch_up_incomplete_session_upgrades,
    combine_result_payloads,
    is_rate_limit_error,
    markdown_report,
    planned_session_seconds,
    report_payload,
    session_supervision_marker_path,
    supervision_marker_complete,
    write_json,
    write_supervision_marker,
)
from paper_trade.run_live_match import selected_agent_commands
from paper_trade.supervisor_ui import default_status


def quote(last=100.0, bid=99.95, ask=100.05):
    return MarketQuote(
        instrument_key="NSE_EQ|TEST",
        symbol="TEST",
        last_price=last,
        best_bid=bid,
        best_ask=ask,
        bid_levels=[QuoteLevel(price=bid, quantity=1000, orders=1)],
        ask_levels=[QuoteLevel(price=ask, quantity=1000, orders=1)],
        open_price=100.0,
        high_price=101.0,
        low_price=99.0,
        close_price=100.0,
        average_price=100.0,
        total_volume=10_000,
    )


class PaperBrokerTest(unittest.TestCase):
    def test_market_buy_fills_against_live_ask(self):
        broker = PaperBroker(PaperConfig(symbol="TEST", instrument_key="NSE_EQ|TEST", starting_cash=10_000))
        broker.set_quote(quote())
        response = broker.submit_order({"user": "bot", "side": "buy", "quantity": 10, "order_type": "market"})
        account = broker.account("bot")

        self.assertEqual(response["status"], "filled")
        self.assertEqual(response["filled_quantity"], 10)
        self.assertGreater(response["average_price"], 100.05)
        self.assertEqual(account["inventory"], 10)
        self.assertEqual(account["fills"], 1)

    def test_post_only_cross_is_rejected(self):
        broker = PaperBroker(PaperConfig(symbol="TEST", instrument_key="NSE_EQ|TEST"))
        broker.set_quote(quote())
        response = broker.submit_order({"user": "bot", "side": "buy", "quantity": 1, "order_type": "limit", "price": 101, "post_only": True})

        self.assertEqual(response["status"], "rejected")
        self.assertIn("post_only", response["reject_reason"])

    def test_passive_limit_fills_when_live_price_trades_through(self):
        broker = PaperBroker(PaperConfig(symbol="TEST", instrument_key="NSE_EQ|TEST", starting_cash=10_000))
        broker.set_quote(quote(last=100.0, bid=99.95, ask=100.05))
        response = broker.submit_order({"user": "bot", "side": "buy", "quantity": 5, "order_type": "limit", "price": 99.90, "post_only": True})
        self.assertEqual(response["status"], "open")

        broker.set_quote(quote(last=99.85, bid=99.80, ask=99.90))
        account = broker.account("bot")
        orders = broker.list_orders(owner="bot")

        self.assertEqual(orders[0]["status"], "filled")
        self.assertEqual(account["inventory"], 5)

    def test_account_funding_updates_initial_cash(self):
        broker = PaperBroker(PaperConfig(symbol="TEST", instrument_key="NSE_EQ|TEST", starting_cash=1_000))
        broker.set_quote(quote())
        account = broker.fund_account("bot", 250)

        self.assertEqual(account["initial_cash"], 1250)
        self.assertEqual(account["cash"], 1250)


class RaiderCoreRiskTest(unittest.TestCase):
    def test_trade_analyzer_ignores_seen_trades(self):
        analyzer = raider_core.TradeAnalyzer(window=10)
        trades = [
            {"id": "t1", "side": "buy", "quantity": 5, "price": 100.0},
            {"id": "t1", "side": "buy", "quantity": 5, "price": 100.0},
            {"id": "t2", "side": "sell", "quantity": 1, "price": 100.1},
        ]

        analyzer.record_trades(trades)
        analyzer.record_trades(trades)

        self.assertEqual(list(analyzer.trades), [("buy", 5.0), ("sell", 1.0)])
        self.assertAlmostEqual(analyzer.sentiment(), 4 / 6)

    def test_dynamic_limits_scale_down_for_small_accounts(self):
        args = argparse.Namespace(
            starting_cash=5000.0,
            max_notional=100000.0,
            max_notional_fraction=1.0,
            max_pos=600.0,
            order_notional=20000.0,
            order_notional_fraction=0.22,
            order_size=40.0,
            min_quantity=1.0,
        )

        max_pos, order_size = raider_core.dynamic_limits(
            args,
            {"initial_cash": 5000.0, "equity": 5000.0},
            mid=100.0,
        )

        self.assertEqual(max_pos, 50.0)
        self.assertEqual(order_size, 11.0)

    def test_quote_sizes_respect_cash_and_loss_pressure(self):
        args = argparse.Namespace(min_quantity=1.0)

        buy_size, sell_size = raider_core.quote_sizes(
            args,
            {"inventory": 0.0, "cash": 500.0},
            bid_price=100.0,
            order_size=20.0,
            max_pos=100.0,
            loss_pressure=1.0,
        )

        self.assertEqual(buy_size, 4.7)
        self.assertEqual(sell_size, 7.0)

    def test_price_tick_ignores_simulation_counter_tick(self):
        self.assertEqual(raider_core.price_tick_from_state({"tick": 5417}, fallback=0.01, mid=100.0), 0.01)
        self.assertEqual(raider_core.price_tick_from_state({"price_tick": 0.05}, fallback=0.01, mid=100.0), 0.05)


class AutoLiveOptimizeTest(unittest.TestCase):
    def test_wait_time_is_zero_inside_market_with_enough_time(self):
        now = dt.datetime(2026, 5, 20, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
        wait = seconds_until_market_live(now, dt.time(9, 15), dt.time(15, 30), min_remaining=300)

        self.assertEqual(wait, 0.0)

    def test_wait_time_skips_weekend(self):
        now = dt.datetime(2026, 5, 23, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
        wait = seconds_until_market_live(now, dt.time(9, 15), dt.time(15, 30), min_remaining=300)

        self.assertGreater(wait, 47 * 60 * 60)
        self.assertLess(wait, 72 * 60 * 60)

    def test_resolve_symbols_rejects_single_symbol_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "symbols.json"
            path.write_text('[{"symbol": "AAA"}, {"symbol": "BBB"}]', encoding="utf-8")

            with self.assertRaises(ValueError):
                resolve_symbols("AAA", symbols_file=path)

            self.assertEqual(resolve_symbols("all", symbols_file=path), ["AAA", "BBB"])

    def test_result_summary_aggregates_across_symbols(self):
        summary = summarize_results(
            {
                "results": [
                    {
                        "symbol": "AAA",
                        "accounts": [
                            {"owner": "BotA", "profit_loss": 10, "max_drawdown": 1, "orders": 2, "fills": 1},
                            {"owner": "BotB", "profit_loss": 4, "max_drawdown": 2, "orders": 3, "fills": 1},
                        ],
                    },
                    {
                        "symbol": "BBB",
                        "accounts": [
                            {"owner": "BotB", "profit_loss": 12, "max_drawdown": 3, "orders": 4, "fills": 2},
                            {"owner": "BotA", "profit_loss": -2, "max_drawdown": 5, "orders": 1, "fills": 0},
                        ],
                    },
                    {"symbol": "CCC", "accounts": [], "error": "quote unavailable"},
                ]
            }
        )

        rows = {row["agent"]: row for row in summary["aggregate"]}
        self.assertEqual(rows["BotA"]["total_pl"], 8.0)
        self.assertEqual(rows["BotA"]["wins"], 1)
        self.assertEqual(rows["BotB"]["wins"], 1)
        self.assertEqual(rows["BotA"]["worst_drawdown"], 5.0)
        self.assertEqual(summary["symbol_errors"], [{"symbol": "CCC", "error": "quote unavailable"}])

    def test_agent_registry_keeps_baseline_and_challenger_active(self):
        registry = default_registry()
        entry = register_challenger(
            registry,
            source_agent="raider_core",
            script_path=Path("example_agents/generated/raider_core_20260521_s02.py"),
            day="20260521",
            stage="30-minute session 2",
            session_index=2,
            supervisor_label="Gemini RaiderCore",
        )

        entries = active_agent_entries(registry, requested_sources={"raider_core"})
        labels = [row["label"] for row in entries]

        self.assertEqual(entry["role"], "challenger")
        self.assertIn("RaiderCore", labels)
        self.assertIn("RaiderCore_S02_G1", labels)

    def test_agent_registry_prunes_losing_challenger_versions(self):
        registry = default_registry()
        loser = register_challenger(
            registry,
            source_agent="raider_core",
            script_path=Path("example_agents/generated/raider_loser.py"),
            day="20260521",
            stage="30-minute session 1",
            session_index=1,
            supervisor_label="Gemini RaiderCore",
        )

        for session_index in (1, 2):
            update_agent_performance(
                registry,
                {
                    "aggregate": [
                        {"agent": loser["label"], "symbols": 2, "wins": 0, "total_pl": -10.0, "worst_drawdown": 4.0},
                        {"agent": "RaiderCore", "symbols": 2, "wins": 2, "total_pl": 1.0, "worst_drawdown": 1.0},
                    ]
                },
                day="20260521",
                stage=f"30-minute session {session_index}",
                session_index=session_index,
            )

        deactivated = prune_losing_agents(registry, requested_sources={"raider_core"}, min_evaluations=2)

        self.assertEqual(deactivated[0]["label"], loser["label"])
        self.assertFalse(loser["active"])
        baseline = next(entry for entry in registry["agents"] if entry["key"] == "raider_core__baseline")
        self.assertTrue(baseline["active"])

    def test_upstox_token_metadata_controls_freshness(self):
        now = dt.datetime.now(dt.UTC)
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / ".upstox_token"
            metadata_file = Path(tmp) / ".upstox_token.json"
            write_access_token(
                token_file,
                {"access_token": "fresh", "expires_at": str(int((now + dt.timedelta(hours=2)).timestamp() * 1000))},
                metadata_file=metadata_file,
                source="test",
            )
            self.assertTrue(token_file_is_fresh(token_file, metadata_file, min_valid_seconds=1800, now=now))

            write_access_token(
                token_file,
                {"access_token": "stale", "expires_at": str(int((now + dt.timedelta(minutes=5)).timestamp() * 1000))},
                metadata_file=metadata_file,
                source="test",
            )
            self.assertFalse(token_file_is_fresh(token_file, metadata_file, min_valid_seconds=1800, now=now))

    def test_live_match_league_mode_launches_registered_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            challenger = root / "raider_candidate.py"
            challenger.write_text("print('candidate')\n", encoding="utf-8")
            registry = default_registry()
            register_challenger(
                registry,
                source_agent="raider_core",
                script_path=challenger,
                day="20260521",
                stage="30-minute session 2",
                session_index=2,
                supervisor_label="Gemini RaiderCore",
            )
            registry_path = root / "agent_registry.json"
            save_agent_registry(registry, registry_path)
            args = argparse.Namespace(
                agents="raider_core",
                league_mode=True,
                agent_registry=str(registry_path),
                max_agent_versions_per_source=4,
                starting_cash=100000.0,
                agent_interval=0.5,
            )

            commands = selected_agent_commands(args, "http://127.0.0.1:8780/api")

        labels = [label for label, _ in commands]
        self.assertIn("RaiderCore", labels)
        self.assertIn("RaiderCore_S02_G1", labels)

    def test_live_match_uses_apex_v5_loop_argument(self):
        args = argparse.Namespace(
            agents="apex_maker",
            league_mode=True,
            agent_registry="paper_trade/agent_registry.json",
            max_agent_versions_per_source=4,
            starting_cash=100000.0,
            agent_interval=0.5,
        )

        commands = selected_agent_commands(args, "http://127.0.0.1:8780/api")
        apex_command = commands[0][1]

        self.assertIn("example_agents/apex_maker_v5.py", apex_command)
        self.assertIn("--target-loop-ms", apex_command)
        self.assertNotIn("--interval", apex_command)

    def test_live_match_continues_after_one_symbol_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_file = Path(tmp) / "result.json"
            args = argparse.Namespace(
                symbols="AAA,BBB",
                keep_results=False,
                result_file=str(result_file),
                continue_on_symbol_error=True,
                duration=30.0,
            )

            def fake_run_symbol(_args, symbol):
                if symbol == "BBB":
                    raise RuntimeError("quote unavailable")
                return {"symbol": symbol, "duration": 30.0, "accounts": [{"owner": "BotA", "profit_loss": 1.0}]}

            with mock.patch.object(run_live_match, "parse_args", return_value=args), mock.patch.object(
                run_live_match, "run_symbol", side_effect=fake_run_symbol
            ), mock.patch.object(run_live_match, "print_ranking"):
                code = run_live_match.main()

            payload = json.loads(result_file.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual([row["symbol"] for row in payload["results"]], ["AAA", "BBB"])
        self.assertEqual(payload["results"][1]["error"], "quote unavailable")

    def test_live_match_returns_nonzero_when_every_symbol_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_file = Path(tmp) / "result.json"
            args = argparse.Namespace(
                symbols="AAA",
                keep_results=False,
                result_file=str(result_file),
                continue_on_symbol_error=True,
                duration=30.0,
            )

            with mock.patch.object(run_live_match, "parse_args", return_value=args), mock.patch.object(
                run_live_match, "run_symbol", side_effect=RuntimeError("quote unavailable")
            ), mock.patch.object(run_live_match, "print_ranking"):
                code = run_live_match.main()

            payload = json.loads(result_file.read_text(encoding="utf-8"))

        self.assertEqual(code, 1)
        self.assertEqual(payload["results"][0]["error"], "quote unavailable")

    def test_daily_supervisor_live_match_continues_on_symbol_error(self):
        args = argparse.Namespace(
            token_file="paper_trade/.upstox_token",
            agents="raider_core",
            host="127.0.0.1",
            starting_cash=100000.0,
            refresh=3.0,
            agent_interval=0.5,
            continue_on_symbol_error=True,
            agent_registry="paper_trade/agent_registry.json",
            max_agent_versions_per_source=4,
            league_mode=True,
        )

        command = daily_supervisor.live_match_command(
            args,
            symbol="AAA",
            duration=60.0,
            port=8780,
            result_file=Path("paper_trade/results/test.json"),
        )

        self.assertIn("--continue-on-symbol-error", command)

    def test_gemini_prompt_contains_no_lookahead_and_multi_symbol_guard(self):
        prompt = build_gemini_prompt(
            result_file=Path("paper_trade/results/test.json"),
            summary={"aggregate": [], "symbol_rankings": []},
            target_agents=["adaptive_edge_maker"],
            competitor_agents=["raider_core", "apex_maker"],
            symbols=["AAA", "BBB"],
        )

        self.assertIn("Do not add look-ahead bias", prompt)
        self.assertIn("Optimize for robust aggregate behavior across the full basket", prompt)
        self.assertIn("example_agents/adaptive_edge_maker.py", prompt)

    def test_daily_supervisor_plans_full_and_partial_sessions(self):
        full = planned_session_seconds(
            3600,
            1800,
            close_buffer=30,
            min_session_seconds=300,
            run_final_partial=True,
        )
        partial = planned_session_seconds(
            700,
            1800,
            close_buffer=30,
            min_session_seconds=300,
            run_final_partial=True,
        )
        skipped = planned_session_seconds(
            700,
            1800,
            close_buffer=30,
            min_session_seconds=300,
            run_final_partial=False,
        )

        self.assertEqual(full, 1800)
        self.assertEqual(partial, 670)
        self.assertEqual(skipped, 0)

    def test_daily_supervisor_combines_result_payloads(self):
        combined = combine_result_payloads(
            [
                {"results": [{"symbol": "AAA"}]},
                {"results": [{"symbol": "BBB"}, {"symbol": "CCC"}]},
            ]
        )

        self.assertEqual([row["symbol"] for row in combined["results"]], ["AAA", "BBB", "CCC"])

    def test_daily_supervisor_detects_upstox_rate_limit(self):
        self.assertTrue(is_rate_limit_error(RuntimeError("Upstox HTTP 429 UDAPI10005 Too Many Request Sent")))
        self.assertFalse(is_rate_limit_error(RuntimeError("Upstox HTTP 500 internal error")))

    def test_daily_supervisor_prompt_separates_agent_ownership(self):
        prompt = build_supervisor_prompt(
            stage="30-minute session 1",
            supervisor_label="Codex AdaptiveEdgeMaker",
            target_agent="adaptive_edge_maker",
            result_files=[Path("paper_trade/results/session.json")],
            summary={"aggregate": []},
            symbols=["AAA", "BBB"],
            session_minutes=30,
            day="20260520",
        )

        self.assertIn("buy / sell / hold", prompt)
        self.assertIn("Do not edit the other supervisor's agent", prompt)
        self.assertIn("Do not add look-ahead bias", prompt)
        self.assertIn("example_agents/adaptive_edge_maker.py", prompt)

    def test_daily_supervisor_prompt_targets_challenger_file(self):
        target_path = daily_supervisor.ROOT / "example_agents" / "generated" / "adaptive_edge_maker_20260521_s02.py"
        prompt = build_supervisor_prompt(
            stage="30-minute session 2",
            supervisor_label="Codex AdaptiveEdgeMaker",
            target_agent="adaptive_edge_maker",
            target_path=target_path,
            result_files=[Path("paper_trade/results/session.json")],
            summary={"aggregate": []},
            symbols=["AAA", "BBB"],
            session_minutes=30,
            day="20260521",
        )

        self.assertIn("Target file: example_agents/generated/adaptive_edge_maker_20260521_s02.py", prompt)
        self.assertIn("Do not edit or recreate the baseline parent file", prompt)

    def test_agent_source_path_falls_back_to_latest_generated_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "example_agents" / "generated"
            generated.mkdir(parents=True)
            older = generated / "adaptive_edge_maker_old.py"
            newer = generated / "adaptive_edge_maker_new.py"
            older.write_text("print('old')\n", encoding="utf-8")
            newer.write_text("print('new')\n", encoding="utf-8")
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))

            with mock.patch.object(daily_supervisor, "ROOT", root), mock.patch.dict(
                daily_supervisor.AGENT_FILES,
                {"adaptive_edge_maker": root / "example_agents" / "adaptive_edge_maker.py"},
            ):
                self.assertEqual(agent_source_path("adaptive_edge_maker"), newer)

    def test_daily_supervisor_report_payload_and_markdown(self):
        payload = report_payload(
            report_type="30_min",
            day="20260520",
            stage="30-minute session 1",
            symbols=["AAA", "BBB"],
            result_files=[Path("paper_trade/results/session.json")],
            summary={
                "aggregate": [
                    {"agent": "BotA", "symbols": 2, "wins": 1, "total_pl": 4.2, "avg_rank": 1.5, "worst_drawdown": 2.0, "orders": 10, "fills": 4}
                ],
                "symbol_rankings": [
                    {"symbol": "AAA", "rank": 1, "agent": "BotA", "profit_loss": 4.2, "max_drawdown": 2.0, "orders": 10, "fills": 4}
                ],
            },
            session_index=1,
            duration=1800,
        )
        markdown = markdown_report(payload)

        self.assertEqual(payload["report_type"], "30_min")
        self.assertIn("30-minute session 1", markdown)
        self.assertIn("BotA", markdown)

    def test_session_supervision_marker_tracks_completed_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = session_supervision_marker_path("20260521", 2, base_dir=Path(tmp))

            self.assertFalse(supervision_marker_complete(marker))
            write_supervision_marker(
                marker,
                day="20260521",
                stage="30-minute session 2",
                session_index=2,
                result_files=[Path("paper_trade/results/session_02.json")],
                summary={"aggregate": []},
            )

            self.assertTrue(supervision_marker_complete(marker))

    def test_catch_up_replays_only_unmarked_session_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "session_02_result.json"
            write_json(result, {"results": [{"symbol": "AAA", "accounts": []}]})
            report_dir = root / "reports"
            session_01 = report_dir / "session_01.json"
            session_02 = report_dir / "session_02.json"
            write_json(
                session_01,
                {
                    "symbols": ["AAA"],
                    "duration_seconds": 1800,
                    "result_files": [str(result)],
                    "summary": {"aggregate": []},
                },
            )
            write_json(
                session_02,
                {
                    "symbols": ["AAA"],
                    "duration_seconds": 1800,
                    "result_files": [str(result)],
                    "summary": {"aggregate": []},
                },
            )

            marker_root = root / "markers"
            write_supervision_marker(
                session_supervision_marker_path("20260521", 1, base_dir=marker_root),
                day="20260521",
                stage="30-minute session 1",
                session_index=1,
                result_files=[result],
                summary={"aggregate": []},
            )
            calls: list[int] = []

            def fake_run_session_supervision(*_args, **kwargs):
                calls.append(kwargs["session_index"])
                write_supervision_marker(
                    session_supervision_marker_path("20260521", kwargs["session_index"], base_dir=marker_root),
                    day="20260521",
                    stage="catch-up",
                    session_index=kwargs["session_index"],
                    result_files=kwargs["result_files"],
                    summary=kwargs["summary"],
                )
                return 0

            args = argparse.Namespace(
                catch_up_session_upgrades=True,
                agents="adaptive_edge_maker,raider_core,apex_maker",
                session_minutes=30.0,
                dry_run_supervisors=False,
            )
            with mock.patch.object(daily_supervisor, "SUPERVISION_MARKERS_DIR", marker_root), mock.patch.object(
                daily_supervisor, "STATUS_FILE", root / "status.json"
            ), mock.patch.object(daily_supervisor, "run_session_supervision", side_effect=fake_run_session_supervision):
                code = catch_up_incomplete_session_upgrades(
                    args,
                    day="20260521",
                    report_files=[session_01, session_02],
                    symbols=["AAA"],
                )

            self.assertEqual(code, 0)
            self.assertEqual(calls, [2])
            self.assertTrue(supervision_marker_complete(session_supervision_marker_path("20260521", 2, base_dir=marker_root)))

    def test_catch_up_defers_supervisor_failure_when_trading_should_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "session_03_result.json"
            write_json(result, {"results": [{"symbol": "AAA", "accounts": []}]})
            report = root / "reports" / "session_03.json"
            write_json(
                report,
                {
                    "symbols": ["AAA"],
                    "duration_seconds": 1800,
                    "result_files": [str(result)],
                    "summary": {"aggregate": []},
                },
            )
            args = argparse.Namespace(
                catch_up_session_upgrades=True,
                continue_on_supervisor_error=True,
                agents="adaptive_edge_maker,raider_core,apex_maker",
                session_minutes=30.0,
                dry_run_supervisors=False,
            )

            with mock.patch.object(daily_supervisor, "SUPERVISION_MARKERS_DIR", root / "markers"), mock.patch.object(
                daily_supervisor, "STATUS_FILE", root / "status.json"
            ), mock.patch.object(daily_supervisor, "run_session_supervision", return_value=42):
                code = catch_up_incomplete_session_upgrades(
                    args,
                    day="20260522",
                    report_files=[report],
                    symbols=["AAA"],
                )

            self.assertEqual(code, 0)
            status = daily_supervisor.load_result(root / "status.json")
            self.assertEqual(status["phase"], "session_upgrade_deferred")

    def test_supervisor_ui_default_status_is_empty(self):
        status = default_status()

        self.assertEqual(status["phase"], "not_started")
        self.assertEqual(status["summary"]["aggregate"], [])


if __name__ == "__main__":
    unittest.main()
