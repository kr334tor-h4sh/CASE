Drop a .gguf model file directly in this folder and CASE's "local" backend
(Settings > Local Models) will auto-detect and load it - no path typing,
no server required. Requires llama-cpp-python to be installed
(pip install llama-cpp-python).

If more than one .gguf file is here, CASE uses the first one alphabetically
unless local_model_path is set explicitly in case_config.json.

This is mainly for the Android/Termux port path, where there's no LM
Studio/Bionic server to call - on the PC, the "remote" backend (talking to
LM Studio/Bionic over HTTP) is the better default since it already handles
model management well.
