module.exports = {
  run: [
    {
      method: "input",
      params: {
        title: "Reset",
        description: "This will delete the conda_env folder and all installed dependencies. You'll need to reinstall before using the app again.",
        type: "modal",
        form: [{
          type: "checkbox",
          key: "confirmed",
          title: "Yes, delete conda_env",
          description: "Remove all installed Python packages and virtual environment"
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
      when: "{{local.confirmed && exists('conda_env')}}",
      method: "shell.run",
      params: {
        path: "app",
        conda: { "path": "{{path.resolve(cwd, 'conda_env')}}" },
        message: "python -c \"from backend.auto_update_config import create_updater; create_updater().save_settings({'mode':'off','frequency':'daily','maintenance_hour':6,'idle_only':True})\""
      }
    },
    {
      when: "{{local.confirmed}}",
      method: "fs.rm",
      params: { path: "auto_update" }
    },
    {
      when: "{{local.confirmed}}",
      method: "fs.rm",
      params: {
        path: "conda_env"
      }
    }
  ]
}
