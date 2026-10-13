export type Mode = "PAPER" | "LIVE";

export interface SymbolTriple {
  tradingview_raw_symbol: string;
  canonical_symbol: string;
  shark_symbol: string;
}

export interface PositionRow {
  position_id: string;
  tradingview_raw_symbol: string;
  canonical_symbol: string;
  shark_symbol: string;
  exchange: string;
  direction: string;
  state: string;
  mode: string;
  strategy_id: string;
  entry_price: string | null;
  mark_price: string | null;
  exit_price: string | null;
  quantity: string;
  margin: string;
  notional: string;
  leverage: number;
  unrealized_pnl: string;
  realized_pnl: string;
  roi_percent: string;
  target_roi_percent: string;
  target_price: string | null;
  entry_fee: string;
  exit_fee: string;
  funding: string;
  open_time: string | null;
  close_time: string | null;
  close_reason: string;
  event_id: string;
  entry_order_id: string | null;
  exit_order_id: string | null;
}

export interface SignalRow {
  id: number;
  event_id: string;
  received_at: string;
  strategy_id: string;
  strategy_name: string;
  tradingview_raw_symbol: string;
  canonical_symbol: string;
  shark_symbol: string;
  action: string;
  source: string;
  timeframe: string;
  alert_price: string | null;
  processing_status: string;
  validation_status: string;
  status_display: string;
  rejection_reason: string;
  position_id: string | null;
  order_id: string | null;
  mode: string;
}

export interface TradeRow {
  trade_id: string;
  position_id: string;
  strategy_id: string;
  strategy_name: string;
  mode: string;
  status: string;
  result: string;
  tradingview_raw_symbol: string;
  canonical_symbol: string;
  shark_symbol: string;
  side: string;
  entry_price: string;
  exit_price: string;
  quantity: string;
  margin: string;
  leverage: number;
  entry_fee: string;
  exit_fee: string;
  funding: string;
  gross_pnl: string;
  net_pnl: string;
  roi_percent: string;
  price_change_percent: string;
  duration_seconds: number;
  opened_at: string | null;
  closed_at: string | null;
  close_reason: string;
  event_id: string;
  entry_order_id: string;
  exit_order_id: string;
}

export interface OrderRow {
  order_id: string;
  position_id: string;
  mode: string;
  exchange: string;
  tradingview_raw_symbol: string;
  canonical_symbol: string;
  shark_symbol: string;
  side: string;
  order_type: string;
  entry_exit: string;
  reduce_only: boolean;
  quantity: string;
  avg_fill_price: string | null;
  filled_quantity: string;
  status: string;
  exchange_order_id: string | null;
  submitted_payload: Record<string, unknown> | null;
  error_code: string;
  error_message: string;
  is_dry_run: boolean;
  created_at: string;
}

export interface StatusResponse {
  mode: Mode;
  halted: boolean;
  kill_switch: boolean;
  dry_run: boolean;
  balance?: {
    wallet_balance: string;
    available_margin: string;
    locked_margin: string;
    account_unit: string;
  };
  risk: {

    open_positions: number;
    reservations: number;
    available_slots: number;
    max_open_positions: number;
    margin_per_trade: string;
    leverage: number;
    target_roi_percent: string;
    stop_loss_enabled: boolean;
    short_enabled: boolean;
    account_unit: string;
  };
  pnl: {
    today_pnl: string;
    realized_pnl: string;
    unrealized_pnl: string;
    total_trades: number;
    winning_trades: number;
    losing_trades: number;
    win_rate: number;
    average_profit: string;
    average_loss: string;
  };
}

export interface HealthResponse {
  status: string;
  mode: Mode;
  components: Record<string, string>;
}
