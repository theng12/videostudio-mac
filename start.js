module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        path: "app",
        conda: {
          "path": "{{path.resolve(cwd, 'conda_env')}}"
        },
        env: {
          "PYTHONUNBUFFERED": "1",
          // Diffusers video pipelines hit ops that don't yet have a Metal (MPS)
          // kernel. Without this, those ops raise; with it, they transparently
          // fall back to CPU so generation completes (slower) instead of crashing.
          "PYTORCH_ENABLE_MPS_FALLBACK": "1"
        },
        message: [
          "if [ -f ../service/.installed ]; then echo \"Startup service mode is installed. Use 'Open UI (service)' or uninstall the startup service before using Start.\"; exit 1; fi",
          // Binds on every network interface (LAN, Tailscale, loopback) at a
          // fixed port so other devices on your tailnet/LAN can hit the API
          // directly without going through Pinokio's proxy. Change the port
          // here if 47872 clashes with something else on your machine.
          "python -m uvicorn backend.main:app --host 0.0.0.0 --port 47872"
        ],
        on: [{
          event: "/Uvicorn running on (http:\\/\\/[0-9.:]+)/",
          done: true
        }, {
          event: "/error:/i",
          break: false
        }]
      }
    },
    {
      method: "local.set",
      params: {
        url: "{{input.event[1]}}"
      }
    }
  ]
}
