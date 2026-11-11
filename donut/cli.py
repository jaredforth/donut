import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from .config import check_project, load, role_choice, state_dir, use_profile
from .install_skills import install
from .providers import validate, activate
from .runner import run_agent, run_batch
from .planner_output import ticket_snapshot, show_created_tickets
from .setup import setup, discover
from .ui import banner, welcome, release_version, paint
from . import workspace

try:
    import readline  # noqa: F401  # Makes input() support Up/Down history and line editing; absent on Windows.
except ImportError:
    pass


def plan_session(profile, alias, project, request=None, session=None):
    install(project, Path.home(), session=session)
    lanes = workspace.lanes_dir(project, session)
    folder = workspace.relative(project, lanes)
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="plan-", dir=state))
    print(
        f"Planner: {alias} ({profile.get('model') or profile['engine'] + ' default'})\nProject: {project}\n"
        f"Tickets: {folder}\nLogs: {run}"
    )
    interactive = request is None
    if interactive:
        print("Describe what to plan. Enter /exit to leave (or Ctrl-D; Windows: Ctrl-Z then Enter).")
    history = []
    while True:
        if interactive:
            try:
                request = input(paint("\nPlan > ", "teal")).strip()
            except EOFError:
                return 0
            if request in ("/exit", "/quit"):
                return 0
            if not request:
                continue
        prompt = (
            "Planning only: read skills/donut-planner/SKILL.md and project instructions. "
            "This explicit planner assignment overrides worker-only prohibitions on creating tickets. "
            f"This session's tickets live in {folder}; leave every other tickets folder alone. "
            f"Research the request and write scoped tickets in {folder}to_do/. Inspect existing tickets "
            f"before adding or updating them; preserve work already in progress or done. Inspect {folder}on_hold/ "
            "to reserve held ticket numbers and avoid duplicates. Keep held specifications consistent, but "
            f"release them only on an explicit user request. Inspect {folder}blocked/ too: reserve its numbers, "
            "investigate blockers, but requeue only when the user requests retry and the blocker is addressed. "
            "Never queue work depending on held or blocked tickets. Do not implement, "
            "commit or push. Your final response must be at most two short sentences describing what you did, "
            "or a concise clarification question if needed. Do not repeat ticket lists or technical details; "
            "Donut will list the created tickets. Earlier user requests in this planning session:\n"
            + json.dumps(history)
            + "\nUser request:\n"
            + request
        )
        if history:
            prompt += f"\nPrevious planner output (consult for follow-up context): {run / f'{len(history):03}.log'}"
        before = ticket_snapshot(project, lanes)
        activate(profile)
        try:
            code = run_agent(profile, project, prompt, run / f"{len(history) + 1:03}.log", planner=True)
        finally:
            if profile["engine"] == "ollama":
                activate(profile)
        if code:
            print(paint("PLANNING FAILED\nCheck the output and logs before running workers.", "coral"))
            return code
        history.append(request)
        show_created_tickets(project, before, lanes)
        print(paint("\nPLANNING COMPLETE", "teal"))
        print("Try exiting the shell and running `donut --work next 1" + (f" --session {session}`" if session else "`"))
        if not interactive:
            return 0


def main():
    # Shared flags work both before and after the subcommand.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("-m", "--model", default=argparse.SUPPRESS, help="configured model alias")
    shared.add_argument("-p", "--project", default=argparse.SUPPRESS, help="project path (default: current directory)")
    shared.add_argument(
        "-s",
        "--session",
        default=argparse.SUPPRESS,
        help="tickets folder .donut/SESSION/ (default: the latest session, else swim_lanes/, else a new session)",
    )
    shared.add_argument(
        "--profile",
        default=argparse.SUPPRESS,
        metavar="NAME",
        help="use the accounts, conversations and tickets of profile NAME only (default: the default profile)",
    )
    parser = argparse.ArgumentParser(
        prog="donut",
        parents=[shared],
        description="Your planning model writes the tickets. Your worker model takes them one at a time. Do the thing. Local or hosted coding agents.",
    )
    parser.add_argument("-v", "--version", action="version", version="donut " + release_version())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--plan",
        nargs="?",
        const="",
        metavar="REQUEST",
        help="open interactive planner; optionally plan one quoted request",
    )
    modes.add_argument(
        "--work",
        nargs="*",
        metavar="NEXT_COUNT",
        help="work next COUNT tickets (for example: --work next 1); bare --work runs all",
    )
    parser.add_argument(
        "--checkpoint",
        action="store_true",
        help="with --work: git commit the working tree after every ticket so each step can be reverted",
    )
    parser.add_argument(
        "--review",
        metavar="ALIAS",
        help="with --work: alias that reviews each finished ticket, or off (default: the saved reviewer, else the planner)",
    )
    parser.add_argument(
        "--recover",
        metavar="ALIAS",
        help="with --work: alias that revises blocked tickets and repairs stuck ones, or off (default: the planner)",
    )
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="with --work: do not raise a pull request after the reviewer approves the run",
    )
    parser.add_argument(
        "--review-rounds",
        type=int,
        default=3,
        metavar="N",
        help="with --work: reviews per ticket before it is blocked (default 3)",
    )
    sub = parser.add_subparsers(dest="action")
    init = sub.add_parser(
        "setup", parents=[shared], help="choose local/hosted orchestrator, discover models and set up project"
    )
    init.add_argument("--scan", help="additional directory to scan for model files")
    sub.add_parser("welcome", help="show the installation welcome screen")
    sub.add_parser("models", parents=[shared], help="list configured aliases")
    scan = sub.add_parser("discover", parents=[shared], help="find Ollama models and GGUF/Safetensors files")
    scan.add_argument("--scan", help="directory to scan instead of common model folders")
    for name in ("init", "add-skills"):
        sub.add_parser(name, parents=[shared], help="install planner/worker skills and a session's ticket lanes")
    work = sub.add_parser("do", parents=[shared], help="compatibility form of --work COUNT")
    work.add_argument("next", choices=["next"])
    work.add_argument("count", help="positive number or all")
    plan = sub.add_parser("plan", parents=[shared], help="compatibility form of --plan REQUEST")
    plan.add_argument("request", help="quoted description of the work to plan")
    args = parser.parse_args()
    if args.plan is not None or args.work is not None:
        if args.action:
            parser.error("Use --plan or --work without a subcommand.")
        if args.plan is not None:
            args.action, args.request = "plan", args.plan or None
        else:
            parts = args.work
            if len(parts) == 2 and parts[0] == "next":
                parts = parts[1:]
            if not parts:
                count = "all"
            elif len(parts) == 1 and (parts[0] == "all" or (parts[0].isdecimal() and int(parts[0]) > 0)):
                count = parts[0]
            else:
                parser.error("Use --work next COUNT, where COUNT is a positive integer or all.")
            args.action, args.count = "do", count
    if not args.action:
        args.action = "terminal"
    if args.action == "welcome":
        welcome(installed=True)
        return 0
    project = Path(getattr(args, "project", ".")).expanduser().resolve()
    try:
        if not project.is_dir():
            raise ValueError(f"Project directory does not exist: {project}")
        use_profile(getattr(args, "profile", None) or os.environ.get("DONUT_PROFILE"))
        check_project(project)
        session = getattr(args, "session", None)
        if session is not None and not workspace.valid(session):
            raise ValueError(f"Invalid session name {session!r}: use letters, numbers, '.', '_' or '-'")
        if args.action == "terminal":
            from .terminal import run_terminal

            run_terminal(project, getattr(args, "model", None), session)
            return 0
        if args.action == "setup":
            setup(project, args.scan)
            return 0
        if args.action in ("init", "add-skills"):
            banner()
            install(project, Path.home(), session=session if session is not None else workspace.default(project))
            return 0
        if args.action == "discover":
            banner()
            models, files = discover(args.scan)
            for model in models:
                print("Ollama: " + model)
            for file in files:
                print("Model file: " + file)
            if not models and not files:
                print("No models found in the searched locations.")
            return 0
        config = load()
        if args.action == "models":
            for name, profile in config["aliases"].items():
                print(f"{name:20} {profile['engine']:10} {profile.get('model') or 'provider default'}")
            if not config["aliases"]:
                print("No aliases yet. Run donut setup.")
            return 0
        role = "plan" if args.action == "plan" else "work"
        if getattr(args, "model", None):
            alias = args.model
            if alias not in config["aliases"]:
                raise ValueError("Choose a configured alias with -m, or run donut setup.")
            profile = config["aliases"][alias]
        else:
            # The alias and model last picked with /model in the terminal, or the role's default alias.
            chosen = role_choice(config, role)
            if not chosen:
                raise ValueError("Choose a configured alias with -m, or run donut setup.")
            alias, profile = chosen
        validate(profile)
        banner()
        if session is None:
            session = workspace.default(project)
        if args.action == "do":

            def resolve(flag, saved_role, name):
                """An explicit alias or off, else the saved choice for the role."""
                if flag == "off":
                    return None
                if flag:
                    if flag not in config["aliases"]:
                        raise ValueError(f"Unknown {name} alias: {flag}. Use --{name} ALIAS or --{name} off.")
                    return config["aliases"][flag]
                chosen = role_choice(config, saved_role)
                return chosen[1] if chosen else None

            reviewer = resolve(args.review, "review", "review")
            planner = resolve(args.recover or config.get("recovery"), "plan", "recover")
            for chosen in (reviewer, planner):
                if chosen:
                    validate(chosen)
            if args.review_rounds < 1:
                raise ValueError("--review-rounds must be at least 1")
            return run_batch(
                profile,
                project,
                args.count,
                checkpoint=args.checkpoint,
                reviewer=reviewer,
                review_rounds=args.review_rounds,
                planner=planner,
                lanes=workspace.lanes_dir(project, session),
                pull_request=not args.no_pr and config.get("pull_requests", True) is not False,
            )
        return plan_session(profile, alias, project, args.request, session)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(paint(f"donut: {error}", "coral"), file=sys.stderr)
        return 1
