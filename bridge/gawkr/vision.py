from __future__ import annotations

import base64
import json
import logging

import httpx

log = logging.getLogger("gawkr.vision")

DESCRIBE_SYSTEM = (
    "You are a security-camera analyst. Describe what is visible in the frame "
    "factually and concisely so it can be searched later. Report only what you can "
    "see; do not guess identities or invent details that are not in the image."
)

# The safety assessment is deliberately grounded in concrete, observable ACTIONS —
# never appearance, clothing, race, or a vague sense of 'suspiciousness'.
DESCRIBE_USER = (
    "Camera location: {camera}. Smart-detection types: {types}.\n\n"
    "Return ONLY a JSON object, no prose around it, with these keys:\n"
    '  "summary": one factual sentence describing the scene,\n'
    '  "objects": list of short noun phrases present,\n'
    '  "attributes": list of distinctive details (colors, clothing, vehicle '
    "make/color, carried items, actions, direction of travel),\n"
    '  "notable": boolean, true if an operator would want this flagged,\n'
    '  "weapon": boolean, true ONLY if a firearm, knife, bat or similar weapon '
    "is clearly visible,\n"
    '  "weapon_detail": the weapon seen, or null,\n'
    '  "concerning": boolean, true ONLY for concrete actions such as trying a '
    "door/window handle, prying or forcing entry, peering into a window, "
    "deliberately concealing the face from the camera, or crouching/hiding at an "
    "entry point. Do NOT base this on a person's appearance or clothing,\n"
    '  "concerning_detail": the specific action observed, or null,\n'
    '  "threat_level": one of "none", "low", "medium", "high".'
)

SEQUENCE_USER = (
    "These are {n} still frames sampled in chronological order from a SINGLE security "
    "event at camera \"{camera}\". Judge BEHAVIOR ACROSS THE SEQUENCE, based only on "
    "concrete actions over time (never appearance, clothing, or perceived "
    "'suspiciousness'):\n"
    "- loitering: lingering or waiting with no apparent purpose\n"
    "- pacing or repeated passes back and forth\n"
    "- repeatedly trying/testing a door or window, or peering into one\n"
    "- approaching then retreating, or deliberately concealing identity\n"
    "Return ONLY a JSON object with keys: \"behavior\" (bool: any concrete concerning "
    "behavior across the frames), \"behavior_detail\" (short description or null), "
    "\"loitering\" (bool), \"threat_level\" (\"none\"|\"low\"|\"medium\"|\"high\")."
)

VEHICLE_USER = (
    "Identify the most prominent vehicle in this image. Return ONLY a JSON object: "
    '"make" (manufacturer, or null), "model" (or null), "color" (or null), '
    '"body_type" (sedan/SUV/pickup/van/coupe/hatchback/motorcycle/bus/other, or null), '
    '"confidence" ("low"|"medium"|"high"), "details" (distinctive features such as a '
    "roof rack, damage, decals, or null). Use null when unsure rather than guessing."
)

PLATE_USER = (
    "Read the vehicle license plate in this image. Return ONLY a JSON object with keys: "
    '"plate" (the characters, or null if unreadable), "region" (state/country if visible, '
    'else null), "confidence" ("low" | "medium" | "high").'
)

# 64x64 solid-magenta JPEG (generated with Pillow) -- used only by self_test()
# to probe whether the endpoint is reachable and actually image-capable. The
# prompt below deliberately does NOT state the color: a text-only model that
# never sees the image has no way to guess "magenta" specifically, whereas a
# model that can see it reliably names it. Never stored/logged beyond the
# pass/fail of that check.
_TEST_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb00430005030404040305"
    "04040405050506070c08070707070f0b0b090c110f1212110f111113161c1713"
    "141a1511111821181a1d1d1f1f1f13172224221e241c1e1f1effdb0043010505"
    "050706070e08080e1e1411141e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e"
    "1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1effc0"
    "0011080040004003012200021101031101ffc4001f0000010501010101010100"
    "000000000000000102030405060708090a0bffc400b510000201030302040305"
    "0504040000017d01020300041105122131410613516107227114328191a10823"
    "42b1c11552d1f02433627282090a161718191a25262728292a3435363738393a"
    "434445464748494a535455565758595a636465666768696a737475767778797a"
    "838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7"
    "b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1"
    "f2f3f4f5f6f7f8f9faffc4001f01000301010101010101010100000000000001"
    "02030405060708090a0bffc400b5110002010204040304070504040001027700"
    "0102031104052131061241510761711322328108144291a1b1c109233352f015"
    "6272d10a162434e125f11718191a262728292a35363738393a43444546474849"
    "4a535455565758595a636465666768696a737475767778797a82838485868788"
    "898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4"
    "c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9"
    "faffda000c03010002110311003f00e6a8a28afe863faac28a28a0028a28a002"
    "8a28a0028a28a0028a28a0028a28a0028a28a0028a28a0028a28a0028a28a002"
    "8a28a0028a28a0028a28a0028a28a0028a28a00fffd9"
)
_TEST_PROMPT = (
    "What is the single dominant color that fills this image? Reply with "
    'ONLY this JSON object and nothing else: {"color": "<one word color name>"}'
)
_TEST_COLOR_WORDS = ("magenta", "pink", "fuchsia", "purple", "violet", "orchid", "plum", "lilac")


class VisionClient:
    def __init__(self, cfg):
        self.cfg = cfg
        headers = {"Content-Type": "application/json"}
        if cfg.vision_api_key:
            headers["Authorization"] = f"Bearer {cfg.vision_api_key}"
        self._client = httpx.AsyncClient(timeout=cfg.vision_timeout, headers=headers)

    async def _chat(self, jpeg: bytes, text: str, max_tokens: int) -> str:
        b64 = base64.b64encode(jpeg).decode("ascii")
        payload = {
            "model": self.cfg.vision_model,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        }
        r = await self._client.post(self.cfg.vision_url, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def describe(self, jpeg: bytes, types: list[str], camera: str = "",
                       context: str = "") -> dict:
        prompt = DESCRIBE_SYSTEM
        if context:
            # Additive background only -- never replaces the JSON-schema
            # instructions below, which must stay the last thing the model reads.
            prompt += ("\n\nSite-specific context from the operator (background only; "
                      "still report only what you observe in this image): " + context)
        prompt += "\n\n" + DESCRIBE_USER.format(
            camera=camera or "unknown", types=", ".join(types) or "motion")
        data = _parse(await self._chat(jpeg, prompt, 450))
        data.setdefault("summary", "")
        data.setdefault("objects", [])
        data.setdefault("attributes", [])
        data.setdefault("notable", False)
        data.setdefault("weapon", False)
        data.setdefault("weapon_detail", None)
        data.setdefault("concerning", False)
        data.setdefault("concerning_detail", None)
        data.setdefault("threat_level", "none")
        return data

    async def read_vehicle(self, jpeg: bytes) -> dict:
        data = _parse(await self._chat(jpeg, VEHICLE_USER, 120))
        for k in ("make", "model", "color", "body_type", "details"):
            data.setdefault(k, None)
        data.setdefault("confidence", "low")
        return data

    async def read_plate(self, jpeg: bytes) -> dict:
        data = _parse(await self._chat(jpeg, PLATE_USER, 80))
        data.setdefault("plate", None)
        data.setdefault("region", None)
        data.setdefault("confidence", "low")
        return data

    async def assess_sequence(self, frames: list[bytes], camera: str = "") -> dict:
        import base64
        content = [{"type": "text",
                    "text": SEQUENCE_USER.format(n=len(frames), camera=camera or "unknown")}]
        for fr in frames:
            b64 = base64.b64encode(fr).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        payload = {"model": self.cfg.vision_model, "temperature": 0.2,
                   "max_tokens": 250, "messages": [{"role": "user", "content": content}]}
        r = await self._client.post(self.cfg.vision_url, json=payload)
        r.raise_for_status()
        data = _parse(r.json()["choices"][0]["message"]["content"])
        data.setdefault("behavior", False)
        data.setdefault("behavior_detail", None)
        data.setdefault("loitering", False)
        data.setdefault("threat_level", "none")
        return data

    async def self_test(self) -> bool:
        """Cheap reachability + image-capability probe for the doctor: send a
        solid-color test image and require the reported color to match --
        the prompt never states the color, so a text-only model with no
        mmproj (which sees no image at all) has nothing to guess it from.
        Raises on connection/HTTP failure; returns False (doesn't raise) when
        reachable but the color doesn't match -- typically no mmproj."""
        content = await self._chat(_TEST_JPEG, _TEST_PROMPT, 30)
        color = str(_parse(content).get("color", "")).lower()
        return any(word in color for word in _TEST_COLOR_WORDS)

    async def close(self) -> None:
        await self._client.aclose()


def _parse(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except Exception:
        log.debug("model did not return parseable JSON")
        return {}
