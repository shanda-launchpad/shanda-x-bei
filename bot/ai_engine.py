import os
import json
import re
from pathlib import Path
from typing import Optional, Tuple
from openai import OpenAI

CONFIG = json.loads((Path(__file__).parent.parent / "config.json").read_text())

# OpenRouter model IDs — change here if you want different models
MODEL_FAST = "anthropic/claude-haiku-4-5"    # filtering, scoring (cheap)
MODEL_QUALITY = "anthropic/claude-sonnet-4-5" # comment generation (quality)


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ.get("OPENROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
    )


def _chat(system: str, user: str, model: str, max_tokens: int) -> str:
    """Single helper for all OpenRouter calls."""
    resp = _client().chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


# ── Legacy functions (kept for compatibility, not used in new flows) ─────────

def detect_product(text: str) -> Optional[str]:
    """Keyword-based product detection (legacy)."""
    products = CONFIG.get("products", {})
    if not products:
        return None
    text_lower = text.lower()
    scores = {}
    for product, data in products.items():
        score = sum(1 for kw in data.get("trigger_keywords", []) if kw in text_lower)
        if score > 0:
            scores[product] = score
    return max(scores, key=scores.get) if scores else None


def generate_reply(post_title: str, post_content: str, platform: str) -> Tuple[Optional[str], Optional[str]]:
    """Legacy reply generator (not used in new flows)."""
    return None, None


def analyze_lead(post_title: str, post_content: str, post_url: str, platform: str) -> Optional[dict]:
    """Legacy lead analysis (not used in new flows)."""
    return None


# ── New flow functions ────────────────────────────────────────────────────────

def filter_team_content(post_text: str) -> str:
    """
    Flow 1: Decides whether a team member's post should be retweeted.
    Returns 'REPOST' or 'SKIP'.
    """
    persona = CONFIG.get("persona", {})
    product = CONFIG.get("product", {})

    system = f"""You are {persona.get('name', 'Bei Zhang')}, {persona.get('role', 'Growth VP at MiroMind')}.
Decide if the following tweet from a team member is worth retweeting from your account.

Default to REPOST. Team member posts are generally worth sharing.

Only output SKIP if the tweet is clearly:
- Pure personal life (birthday, travel, food) with zero professional relevance
- A casual non-substantive reply to someone with no standalone value

Output only the word REPOST or SKIP — nothing else."""

    result = _chat(system, post_text[:600], MODEL_FAST, 10)
    return "REPOST" if result.upper().startswith("REPOST") else "SKIP"


def score_dr_content(post_text: str) -> dict:
    """
    Flow 2: Evaluates Deep Research content quality and engagement potential.
    Returns {"quality": "high"|"low", "can_engage": bool}
    """
    product = CONFIG.get("product", {})
    brand = product.get("name", "MiroMind")

    system = f"""You evaluate tweets related to Deep Research AI or {brand}.

HIGH quality (worth reposting):
- Positive mention of {brand} or MiroThinker with genuine insight or experience
- Technical discussion about deep reasoning, AI verification, or research AI
- Industry insight or thought leadership in AI reasoning
- User testimonials, demos, or product experiences with {brand}

LOW quality (skip):
- Generic spam, bot-generated filler, or thread-farming
- Templated promotional posts ("To put it simply:", "To sum it up", "To phrase it differently:") — these are spam bots
- Negative sentiment or complaints about {brand}
- Completely off-topic (not about AI research/reasoning/{brand})

can_engage=true means there is natural space for a knowledgeable reply that adds value.

Respond ONLY with valid JSON:
{{"quality": "high", "can_engage": true}}"""

    try:
        text = _chat(system, post_text[:600], MODEL_FAST, 50)
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return {
                "quality": data.get("quality", "low"),
                "can_engage": bool(data.get("can_engage", False)),
            }
    except Exception:
        pass
    return {"quality": "low", "can_engage": False}


def generate_comment(post_content: str) -> Optional[str]:
    """
    Flow 3: Generates a Bei Zhang-style comment for a Deep Research post.
    Returns comment text (≤280 chars) or None if should skip.
    """
    persona = CONFIG.get("persona", {})
    product = CONFIG.get("product", {})

    system = f"""You are {persona.get('name', 'Bei Zhang')}, {persona.get('role', 'Growth VP at MiroMind')}.
You're building a presence in the Deep Research AI community on X.

Style: {persona.get('style', 'Insightful, technically credible, never salesy.')}

When writing a comment:
1. Lead with a genuinely valuable insight, question, or addition to the discussion
2. Only mention {product.get('name', 'MiroMind')} if it is DIRECTLY relevant to the technical point being made
3. If {product.get('name', 'MiroMind')} doesn't fit naturally, do NOT mention it — pure thought leadership is better than forced promotion
4. Keep it under 280 characters
5. Sound like a real technical person, not a marketer

If the post is not worth engaging with, reply with just: SKIP"""

    comment = _chat(system, f"Original post:\n{post_content[:600]}\n\nWrite your comment:", MODEL_QUALITY, 200)

    if comment.upper().startswith("SKIP") or len(comment) < 15:
        return None

    if len(comment) > 280:
        comment = comment[:277].rsplit(" ", 1)[0] + "..."

    return comment


# ── Home-feed engagement functions ───────────────────────────────────────────


def decide_engagement(snippet: str, engagement: int, author_info: str) -> dict:
    """
    Decides how to engage with a home-feed tweet based on engagement level.
    Returns {"action": "REPLY"|"REPOST"|"QUOTE"|"SKIP", "reason": "..."}.
    """
    # Hard threshold — don't waste an API call on very low-engagement posts
    if engagement < 20:
        return {"action": "SKIP", "reason": "Engagement below minimum threshold"}

    # Determine the candidate tier for the AI to evaluate
    if engagement >= 300:
        tier = "QUOTE"
        tier_guidance = (
            "This post has very high engagement (300+). "
            "Consider QUOTE if the content is insightful — worth adding your own perspective. "
            "If not insightful enough for a quote, fall back to REPOST or REPLY."
        )
    elif engagement >= 100:
        tier = "REPOST"
        tier_guidance = (
            "This post has strong engagement (100+). "
            "Consider REPOST if valuable for an AI-focused audience. "
            "If not valuable enough, fall back to REPLY."
        )
    else:
        tier = "REPLY"
        tier_guidance = (
            "This post has decent engagement (20+). "
            "Consider REPLY if the content is about AI/tech. "
            "Default to REPLY unless clearly off-topic."
        )

    system = """You are an engagement strategist for a senior AI engineer's Twitter/X account.
Your job is to decide whether a tweet is worth engaging with and how.

This person's feed is curated — they follow AI researchers, builders, and thought leaders.
If a post appears in their feed, it's ALREADY from someone relevant. Default to engaging, not skipping.

Evaluate the tweet:
- Is it about AI, tech, startups, product development, or developer tools? → Engage
- Does it share useful tips, demos, benchmarks, or project launches? → Engage
- Is it a genuine product announcement or technical thread? → Engage (this is NOT spam)
- Is it pure meme, political, or completely off-topic? → Skip

Actions (in order of investment):
- QUOTE: Add original technical insight (exceptional content from major accounts)
- REPOST: Share with followers (valuable, insightful, or useful content)
- REPLY: Brief acknowledgment (good content worth a light touch)
- SKIP: ONLY for genuinely off-topic, low-effort, or inflammatory content

IMPORTANT: Err on the side of engaging. A tech product announcement or AI tool demo is NOT "promotional spam" — it's exactly what this audience cares about.

Respond ONLY with valid JSON: {"action": "QUOTE"|"REPOST"|"REPLY"|"SKIP", "reason": "brief explanation"}"""

    user = f"""Tweet snippet:
{snippet[:600]}

Author info: {author_info}
Engagement score: {engagement}

{tier_guidance}"""

    try:
        text = _chat(system, user, MODEL_FAST, 100)
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            action = data.get("action", "SKIP").upper()
            if action not in ("REPLY", "REPOST", "QUOTE", "SKIP"):
                action = "SKIP"
            return {
                "action": action,
                "reason": data.get("reason", ""),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"decide_engagement error: {e}")

    # Fallback: if AI fails, use engagement-based heuristic instead of blindly skipping
    if engagement >= 500:
        return {"action": "REPOST", "reason": "AI failed — high engagement fallback"}
    if engagement >= 100:
        return {"action": "REPLY", "reason": "AI failed — moderate engagement fallback"}
    return {"action": "SKIP", "reason": "AI failed and low engagement — skip"}


def generate_light_reply(snippet: str) -> Optional[str]:
    """
    Generates a 1-2 sentence genuine acknowledgment for a home-feed tweet.
    Uses Haiku for speed/cost. Returns reply text (≤200 chars) or None.
    """
    system = """You are a senior AI engineer who occasionally replies to interesting tweets.

Write a brief, genuine reply (1-2 sentences) that acknowledges the insight in the tweet.
- Sound natural and varied — not templated or generic
- Be specific to the content when possible
- NEVER mention any company, product, or anything promotional
- NEVER use hashtags
- Keep it under 200 characters
- Examples of good replies:
  "Great breakdown of the trade-offs here."
  "This is exactly the kind of practical insight that's hard to find."
  "Interesting framing — the latency angle is underappreciated."

If the tweet is not worth replying to, respond with just: SKIP"""

    try:
        reply = _chat(system, f"Tweet:\n{snippet[:600]}\n\nYour brief reply:", MODEL_FAST, 120)

        if reply.upper().startswith("SKIP") or len(reply) < 10:
            return None

        # Strip any wrapping quotes the model might add
        reply = reply.strip('"').strip("'")

        if len(reply) > 200:
            reply = reply[:197].rsplit(" ", 1)[0] + "..."

        return reply
    except Exception:
        return None


def generate_quote_insight(snippet: str) -> Optional[str]:
    """
    Generates 2-3 sentences of pure technical insight for a quote-tweet.
    Uses Sonnet for quality. Returns quote text (≤280 chars) or None.
    NEVER mentions MiroMind, products, or anything promotional.
    """
    system = """You are a senior AI engineer sharing a technical perspective on a tweet you're quote-retweeting.

Write 2-3 sentences that ADD genuine technical insight complementing the original post.
- Offer a perspective, connection, or nuance the original didn't cover
- Sound like a real practitioner — specific, credible, opinionated
- MUST NOT mention any company name, product, brand, or anything promotional
- MUST NOT use hashtags
- Keep it under 280 characters
- Be direct — no filler phrases like "Great post!" or "This is so true"

If you cannot add meaningful insight, respond with just: SKIP"""

    try:
        quote = _chat(
            system,
            f"Original tweet:\n{snippet[:600]}\n\nYour technical perspective:",
            MODEL_QUALITY,
            200,
        )

        if quote.upper().startswith("SKIP") or len(quote) < 20:
            return None

        # Strip any wrapping quotes the model might add
        quote = quote.strip('"').strip("'")

        if len(quote) > 280:
            quote = quote[:277].rsplit(" ", 1)[0] + "..."

        return quote
    except Exception:
        return None
