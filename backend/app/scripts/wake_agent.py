"""Check the selected Director provider and optionally reload project context.

Local exclusive mode warms/releases Ollama; independent mode never changes
LLM or Comfy residency. Run from backend with python -m app.scripts.wake_agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _run(project_id: str | None, *, release: bool, keep: bool) -> int:
    from app.agents.director.context_io import load_agent_context
    from app.core.llm import get_llm_provider
    from app.core.vram import get_orchestrator

    orch = get_orchestrator()
    from app.core.vram.director_model import get_director_model

    provider = get_llm_provider()
    model = get_director_model()
    print(f"wake: model={model} provider={provider.provider_id} policy={orch.policy}", flush=True)
    if orch.shared_gpu_enabled and orch.owner == "comfy":
        print(
            f"wake: WARNING GPU owner=comfy pipeline={orch.comfy_pipeline}. "
            "Wait for Comfy job to finish (exclusive VRAM) or wake will be slow/timeout.",
            flush=True,
        )

    print("wake: checking selected model availability…", flush=True)
    async with orch.llm_session(release_on_exit=not keep and not release):
        await orch.ensure_llm_ready()
        print("wake: selected model is available", flush=True)


        if project_id:
            ctx = load_agent_context(project_id)
            if ctx is None:
                print(f"wake: no context for project {project_id}", flush=True)
            else:
                print(
                    f"wake: loaded context project={ctx.project_id} "
                    f"phase={ctx.last_phase} shots={len(ctx.shot_summaries)} "
                    f"models={ctx.models_used}",
                    flush=True,
                )
                # Touch generate with a tiny context-bound prompt so the model
                # reloads conversational context without inventing a plan.
                summary = json.dumps(
                    {
                        "project_id": ctx.project_id,
                        "last_phase": ctx.last_phase,
                        "shot_ids": [s.get("id") for s in ctx.shot_summaries[:12]],
                    },
                    ensure_ascii=False,
                )
                text = await provider.client.generate(
                    model,
                    "You are the Director Studio local agent. "
                    "Acknowledge context reload in one short sentence.\n"
                    f"CONTEXT:\n{summary}\n",
                )
                print(f"wake: agent reply: {text.strip()[:300]}", flush=True)

        if not orch.shared_gpu_enabled:
            print("wake: independent provider manages its own residency", flush=True)
        elif release:
            await orch.release_llm()
            print("wake: released LLM (VRAM free for Comfy)", flush=True)
        elif keep:
            print("wake: keeping LLM resident (owner=llm until next release)", flush=True)
        else:
            # default: session exit releases
            print("wake: session end will release LLM", flush=True)

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check Director provider + reload context")
    p.add_argument("--project", help="Project id to reload agent/context.json for")
    p.add_argument(
        "--release",
        action="store_true",
        help="Unload model after wake (smoke / free VRAM)",
    )
    p.add_argument(
        "--keep",
        action="store_true",
        help="Leave model loaded after wake (do not unload on exit)",
    )
    args = p.parse_args(argv)
    if args.release and args.keep:
        print("error: use only one of --release / --keep", file=sys.stderr)
        return 2
    return asyncio.run(_run(args.project, release=args.release, keep=args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
