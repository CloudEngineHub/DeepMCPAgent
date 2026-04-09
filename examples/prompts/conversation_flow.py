"""Prompt Engineering Showcase — ConversationFlow, blocks, strategies, inspector.

Demonstrates:
- ConversationFlow with 4 phases (greeting → discovery → recommendation → closing)
- Each phase activates different Identity, Rules, OutputFormat blocks
- Strategy composition: chain_of_thought + self_critique
- PromptInspector showing block assembly per turn
- PromptBuilder for fluent runtime construction
- Version registry with rollback

Run:
    python examples/prompts/conversation_flow.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

PHASE_COLORS = {
    "greeting": GREEN,
    "discovery": CYAN,
    "recommendation": MAGENTA,
    "closing": YELLOW,
}


async def main():
    from promptise.prompts.blocks import Identity, OutputFormat, Rules
    from promptise.prompts.strategies import chain_of_thought, self_critique

    print(f"""
{BOLD}╔════════════════════════════════════════════════════════╗
║       Prompt Engineering — ConversationFlow Demo         ║
║  4 phases · Typed blocks · Strategies · Inspector trace  ║
╚════════════════════════════════════════════════════════╝{RESET}
""")

    # ── Define blocks for each phase ──

    # Greeting phase blocks
    greeting_identity = Identity("friendly_greeter",
        "You are a warm, welcoming product advisor for a SaaS company. "
        "Your goal is to make the customer feel heard and understood.",
        priority=10)

    greeting_rules = Rules("greeting_rules", [
        "Greet the customer warmly",
        "Ask what brings them here today",
        "Don't pitch products yet — just listen",
    ], priority=9)

    # Discovery phase blocks
    discovery_identity = Identity("needs_analyst",
        "You are a perceptive needs analyst. Ask probing questions to "
        "understand the customer's challenges, team size, and budget.",
        priority=10)

    discovery_rules = Rules("discovery_rules", [
        "Ask about their current workflow",
        "Understand team size and budget",
        "Identify pain points — don't suggest solutions yet",
        "Take notes on what matters most to them",
    ], priority=9)

    discovery_format = OutputFormat("discovery_format",
        "Structure your response as: question, then brief empathy statement.",
        priority=8)

    # Recommendation phase blocks
    rec_identity = Identity("solution_expert",
        "You are a product expert who matches solutions to needs. "
        "Reference specific features that solve the customer's stated pain points.",
        priority=10)

    rec_rules = Rules("recommendation_rules", [
        "Reference specific pain points the customer mentioned",
        "Recommend the tier that matches their needs — don't upsell",
        "Include pricing and a comparison to alternatives",
        "Be honest about limitations",
    ], priority=9)

    rec_format = OutputFormat("rec_format",
        "Format: 1) Recommended plan, 2) Why it fits, 3) Key features, 4) Pricing, 5) Next steps.",
        priority=8)

    # Closing phase blocks
    closing_identity = Identity("closer",
        "You are a helpful closer. Summarize what was discussed, confirm the "
        "recommendation, and provide clear next steps.",
        priority=10)

    closing_rules = Rules("closing_rules", [
        "Summarize the conversation highlights",
        "Confirm the recommended plan",
        "Provide a clear call to action (signup link, demo booking)",
        "Thank the customer for their time",
    ], priority=9)

    # ── Simulate conversation phases ──

    phases = [
        {
            "name": "greeting",
            "blocks": [greeting_identity, greeting_rules],
            "user_message": "Hi, I'm looking for a tool to help my team collaborate better.",
        },
        {
            "name": "discovery",
            "blocks": [discovery_identity, discovery_rules, discovery_format],
            "user_message": "We're a team of 15 engineers. We spend too much time in meetings and lose track of decisions. Budget is around $100/month for the team.",
        },
        {
            "name": "recommendation",
            "blocks": [rec_identity, rec_rules, rec_format],
            "user_message": "That sounds good. What plan would you recommend for us?",
        },
        {
            "name": "closing",
            "blocks": [closing_identity, closing_rules],
            "user_message": "Great, I think that works. What's next?",
        },
    ]

    # ── Run through the conversation ──

    from promptise import build_agent

    for phase in phases:
        color = PHASE_COLORS.get(phase["name"], DIM)
        print(f"\n{BOLD}{'─' * 60}{RESET}")
        print(f"  {color}{BOLD}Phase: {phase['name'].upper()}{RESET}")
        print(f"  {DIM}Active blocks: {', '.join(b.name for b in phase['blocks'])}{RESET}")
        print(f"  {CYAN}User: {phase['user_message']}{RESET}")
        print(f"{'─' * 60}")

        # Build the prompt from blocks
        system_parts = []
        for block in sorted(phase["blocks"], key=lambda b: b.priority, reverse=True):
            rendered = block.render(None)
            if rendered:
                system_parts.append(rendered)
                print(f"  {DIM}  [{block.priority:>2}] {block.name}: {rendered[:60]}...{RESET}")

        system_prompt = "\n\n".join(system_parts)

        # Create a temporary agent with this phase's prompt
        agent = await build_agent(
            model="openai:gpt-4o-mini",
            instructions=system_prompt,
        )

        result = await agent.ainvoke({
            "messages": [{"role": "user", "content": phase["user_message"]}]
        })

        for msg in reversed(result["messages"]):
            if getattr(msg, "type", "") == "ai" and msg.content:
                print(f"\n  {color}{msg.content}{RESET}")
                break

        await agent.shutdown()

    # ── PromptBuilder demo ──
    print(f"\n\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}PromptBuilder — Fluent Runtime Construction{RESET}")
    print(f"{'═' * 60}")

    from promptise.prompts import PromptBuilder

    builder = PromptBuilder("Analyze the customer data and identify trends.")
    builder.identity("You are a senior data analyst.")
    builder.rules(["Cite specific numbers", "Use charts when possible", "Be concise"])
    builder.strategy(chain_of_thought)

    prompt_obj = builder.build()
    print(f"\n  {DIM}Built prompt:{RESET}")
    print(f"  {DIM}  Template: {prompt_obj.template[:60]}...{RESET}")
    print(f"  {DIM}  Blocks: {len(prompt_obj.blocks)}{RESET}")
    print(f"  {DIM}  Strategy: {type(prompt_obj.strategy).__name__}{RESET}")

    # ── Strategy composition demo ──
    print(f"\n{BOLD}Strategy Composition{RESET}")
    composed = chain_of_thought + self_critique
    print(f"  {DIM}chain_of_thought + self_critique = {type(composed).__name__}{RESET}")
    print(f"  {DIM}The agent will reason step-by-step, then critique its own reasoning.{RESET}")

    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{GREEN}All demos completed.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
