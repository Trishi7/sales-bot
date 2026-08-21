// PM2 process definition for the sales bot.
//
//   pm2 start ecosystem.config.js
//   pm2 logs sales-bot
//   pm2 restart sales-bot
//
// The process name is `sales-bot`. It is deliberately NOT the PM bot's name —
// the two run side by side on the same box, each with its own venv, its own
// .env, its own SQLite file and its own Discord application. Nothing is shared.
//
// See DEPLOY.md for the full deployment, including the server-side channel
// permissions that are the FIRST layer of the scoping rule.
module.exports = {
  apps: [
    {
      name: 'sales-bot',
      // The venv interpreter, not the system python: the bot's dependencies live
      // in ./venv and the system python cannot see them.
      interpreter: './venv/bin/python',
      script: 'main.py',
      cwd: __dirname,

      // One process. This bot holds a single Discord gateway connection and a
      // local SQLite file; a second instance would double every nudge and race
      // on the database.
      instances: 1,
      exec_mode: 'fork',

      // Config comes from .env via python-dotenv (config.py), NOT from here, so
      // there is exactly one place secrets live and it is never committed.
      autorestart: true,
      max_restarts: 10,
      // Anything that dies within 30s of starting counts as a crash loop rather
      // than a restart — usually a bad token or a missing env var, and hammering
      // Discord with reconnects makes that worse.
      min_uptime: '30s',
      restart_delay: 5000,

      // Restart if it balloons — a leak in a long-lived gateway process is far
      // more likely than a legitimate 500MB working set here.
      max_memory_restart: '500M',

      // Logs land in ./logs, which .gitignore excludes.
      out_file: './logs/sales-bot.out.log',
      error_file: './logs/sales-bot.err.log',
      merge_logs: true,
      time: true,

      env: {
        PYTHONUNBUFFERED: '1', // so logs reach PM2 as they happen, not on exit
      },
    },
  ],
};
