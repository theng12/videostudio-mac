module.exports = {
  run: [
    {
      method: "input",
      params: {
        title: "Update & Restart",
        description: "This will stop the server, pull the latest code, install dependencies, and restart.",
        type: "modal",
        form: [{
          type: "checkbox",
          key: "confirmed",
          title: "Yes, proceed",
          description: "Stop the server → update → restart"
        }]
      }
    },
    {
      method: "local.set",
      params: {
        confirmed: "{{input.confirmed}}"
      }
    },
    {
      when: "{{local.confirmed}}",
      method: "notify",
      params: {
        title: "Update & Restart",
        message: "Stopping server…"
      }
    },
    {
      when: "{{local.confirmed}}",
      method: "script.stop",
      params: {
        uri: "start.js"
      }
    },
    {
      when: "{{local.confirmed}}",
      method: "shell.run",
      params: {
        message: "sleep 3"
      }
    },
    {
      when: "{{local.confirmed}}",
      method: "shell.run",
      params: {
        message: "git pull"
      }
    },
    {
      when: "{{local.confirmed && exists('conda_env')}}",
      method: "shell.run",
      params: {
        path: "app",
        conda: {
          "path": "{{path.resolve(cwd, 'conda_env')}}"
        },
        message: [
          "python -m pip install --upgrade pip",
          "uv pip install -r requirements.txt"
        ]
      }
    },
    {
      when: "{{local.confirmed && exists('service/.installed')}}",
      method: "shell.run",
      params: {
        message: [ "bash restart_service.sh" ]
      }
    },
    {
      when: "{{local.confirmed}}",
      method: "notify",
      params: {
        title: "Update & Restart",
        message: "Starting server…"
      }
    },
    {
      when: "{{local.confirmed}}",
      method: "script.start",
      params: {
        uri: "start.js"
      }
    }
  ]
}
