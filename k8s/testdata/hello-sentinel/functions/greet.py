def run(args: dict) -> dict:
    who = args.get("who", "world")
    return {"message": f"hello, {who}"}
