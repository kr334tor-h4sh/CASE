"""
CASE - terminal client. Run this on the PC.

Architecture: the agent (this + case_agent.py/case_tools.py - tools, files,
memory) runs on the PC, where the project files actually live. It calls the
Mac mini's Bionic/LM Studio server for the actual model inference. The Mac
mini only ever sees chat messages + tool schemas/results over the LAN - it
never touches the filesystem directly.

This is a standalone client - it only depends on Bionic's underlying API
server, not its own chat UI. If Bionic's GUI has issues but the server
process is still up, this still works.

Usage:
    python case_cli.py
    (type your question, Ctrl+C or "exit" to quit)
"""

import case_agent
import case_tools


def main():
    print("CASE — local fallback assistant (agent: PC)")
    if case_agent.get_backend() == "local":
        print(f"Model: local file — {case_agent.get_local_model_path() or '(none found in ' + str(case_agent.LOCAL_MODELS_DIR) + ')'}")
    else:
        print(f"Model endpoint: {case_agent.get_remote_base()}")
    print("Type your question. Ctrl+C or 'exit' to quit.")
    print("Use /attach <path> to attach a file to your NEXT message.")
    print("Use /model to see the currently loaded model and its settings.\n")

    history = case_agent.new_history()
    pending_attachment = None  # set by /attach, consumed by the next real question

    while True:
        try:
            question = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCASE: Operational. Ending session.")
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            print("CASE: Operational. Ending session.")
            break

        if question == "/model":
            info = case_agent.get_model_info_summary()
            if info["backend"] == "local":
                print("CASE: Backend: Local")
                print(f"      Model: {info['model_path'] or '(none selected)'}")
                print(f"      Loaded: {'yes' if info['loaded'] else 'not yet (loads on first message)'}")
                print(f"      Context: {info['n_ctx'] if info['n_ctx'] is not None else '-'}")
                if not info["gpu_backend_available"]:
                    gpu_line = "no GPU backend available"
                elif info["n_gpu_layers"] is None:
                    gpu_line = "not loaded yet"
                elif info["n_gpu_layers"] == -1:
                    gpu_line = "all layers (full offload)"
                elif info["n_gpu_layers"] == 0:
                    gpu_line = "CPU only"
                else:
                    gpu_line = f"{info['n_gpu_layers']} layers"
                print(f"      GPU offload: {gpu_line}")
                print(f"      GPU: {info['gpu_name'] or 'not detected'}\n")
            else:
                print("CASE: Backend: Remote")
                print(f"      Model: {info['model_id']}")
                print(f"      Endpoint: {info['endpoint']}\n")
            continue

        if question.startswith("/attach "):
            path = question[len("/attach "):].strip()
            result = case_tools.read_file_for_attach(path)
            if "error" in result:
                print(f"CASE: Couldn't attach '{path}' — {result['error']}\n")
            else:
                pending_attachment = result
                print(f"CASE: Attached '{result['name']}' — will be included in your next message.\n")
            continue

        message = case_agent.compose_message_with_attachment(question, pending_attachment)
        pending_attachment = None
        try:
            answer = case_agent.ask(message, history)
        except case_agent.CaseUnreachable as e:
            print(f"\nCASE: {e}\n")
            continue
        except Exception as e:
            print(f"\nCASE: Something went wrong — {type(e).__name__}: {e}\n")
            continue

        print(f"\nCASE: {answer}\n")


if __name__ == "__main__":
    main()
