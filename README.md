# Exchange_Bots

Collection of automated cryptocurrency trading bots and execution engines.

## Projects

### 1. [mudrex_auto](./mudrex_auto)
Automated Telegram signal parser and execution bot for Mudrex with paper and live trading modes, position tracking, and real-time dashboard.

### 2. [mudrex_roi](./mudrex_roi)
Mudrex trading bot variant enhanced with custom ROI tracking and target monitoring.

### 3. [Shark_Automate](./Shark_Automate)
Full-stack trading automation platform featuring:
- **Backend:** FastAPI, automated order execution, paper/live trading pipelines, risk management, and Telegram listener.
- **Frontend:** Next.js dashboard for positions, sentiment monitoring, and system settings.

---

## Setup & Configuration

Each project contains its own configuration file template (`.env.example`).
To configure an individual bot:
1. Copy `.env.example` to `.env` in the project directory.
2. Fill in your API keys and configuration parameters.
3. Follow the project-specific README or startup scripts (`start.sh`, `run_paper.sh`, etc.).
