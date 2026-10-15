# Exchange_Bots

Collection of automated cryptocurrency trading bots and execution engines.

## Projects

### 1. [mudrex_auto](./mudrex_auto)
Automated Telegram signal parser and execution bot for Mudrex with paper and live trading modes, position tracking, and real-time dashboard.

### 2. [mudrex_roi](./mudrex_roi)
Mudrex trading bot variant enhanced with custom ROI tracking and target monitoring.

### 3. [pi42_roi](./pi42_roi)
Pi42 (INR/USDT perpetual futures) equivalent of `mudrex_roi`: Telegram signals executed as LIMIT entries carrying an ROI take-profit, PAPER-first with guarded REAL mode, and no stop-loss. Includes a dependency-free web dashboard and a Tamil user guide.

### 4. [Shark_Automate](./Shark_Automate)
Full-stack trading automation platform featuring:
- **Backend:** FastAPI, automated order execution, paper/live trading pipelines, risk management, and Telegram listener.
- **Frontend:** Next.js dashboard for positions, sentiment monitoring, and system settings.

### 4. [Shasha_Act](./Shasha_Act)
PAPER-first TradingView SHASHA signal receiver and execution console for Shark Exchange:
- **Backend:** FastAPI, PostgreSQL outbox, risk gates, simulated paper fills with live market data feeds.
- **Frontend:** Single-page real-time dashboard for positions, orders, and risk controls.

---

## Setup & Configuration

Each project contains its own configuration file template (`.env.example`).
To configure an individual bot:
1. Copy `.env.example` to `.env` in the project directory.
2. Fill in your API keys and configuration parameters.
3. Follow the project-specific README or startup scripts (`start.sh`, `run_paper.sh`, etc.).
