def run(args: dict) -> dict:
    text = args.get("text") or ""
    return {"length": len(text)}
