"""SalesPilot registration. Does not touch the network or credentials."""

from __future__ import annotations

from pathlib import Path

try:
    from . import approval, commands, mission, plugin_settings, schemas, tools
except ImportError:  # loaded as a top-level module by the test runner
    import approval
    import commands
    import mission
    import plugin_settings
    import schemas
    import tools

_HANDLERS = {
    "salespilot_onboard_business": tools.onboard_business,
    "salespilot_import_customers": tools.import_customers,
    "salespilot_qualify_lead": tools.qualify_lead,
    "salespilot_research_opportunity": tools.research_opportunity,
    "salespilot_draft_outreach": tools.draft_outreach,
    "salespilot_plan_followups": tools.plan_followups,
    "salespilot_report": tools.report,
    "salespilot_request_approval": tools.request_approval,
    "salespilot_send_email": commands.send_email,
    "salespilot_configure_mail": commands.configure_mail,
    "salespilot_mission": mission.mission,
    "salespilot_pipeline": tools.pipeline_action,
}


def register(ctx) -> None:
    tools.bind(ctx)
    commands.bind(ctx)
    approval.bind(ctx)
    plugin_settings.bind(ctx)
    for schema in schemas.TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="salespilot",
            schema=schema,
            handler=_HANDLERS[schema["name"]],
        )
    # Sending and approve/reject ask the engine's native approval prompt for each exact email.
    ctx.register_hook("pre_tool_call", approval.make_hook(commands.send_preview, tools.ledger_preview))
    ctx.register_hook("post_tool_call", approval.post_tool_call)
    ctx.register_command(
        "salespilot-send",
        commands.slash_send,
        description="Send one approved email draft. Shows the exact email and asks before sending. Never a bulk send.",
        args_hint="<draft_id>",
    )
    skill = Path(__file__).resolve().parent / "skills" / "salespilot" / "SKILL.md"
    ctx.register_skill("salespilot", skill)
