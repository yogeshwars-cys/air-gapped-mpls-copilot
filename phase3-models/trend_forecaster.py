#!/usr/bin/env python3
# =============================================================================
# trend_forecaster.py — Slow-Trend Forecaster (Holt-Winters / Prophet)
#
# Detects gradual, multi-cycle congestion drift that the BiLSTM's short window
# (30 timesteps) misses.  Produces a t+N_HORIZON forecast per telemetry channel.
#
# Default: Holt-Winters double exponential smoothing (statsmodels) — same
# trend-detection value as Prophet, but needs no seasonality history.
# Prophet is gated behind a PROPHET_MIN_SAMPLES threshold — only switched in
# once enough cyclical history exists to make seasonality decomposition valid.
#
# The forecaster output feeds the Predictive Digital Twin (digital_twin.py).
#
# Usage:
#   from trend_forecaster import TrendForecaster
#   fc = TrendForecaster()
#   fc.update("queue_depth_pe1", value)
#   prediction = fc.forecast("queue_depth_pe1")   # returns t+30s estimate
# =============================================================================
import collections
import numpy as np

try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    HAS_STATSMODELS = True
except ImportError:
    HAS_STATSMODELS = False

try:
    from prophet import Prophet
    import pandas as pd
    HAS_PROPHET = True
except ImportError:
    HAS_PROPHET = False

N_HORIZON       = 30    # forecast steps ahead (seconds at 1s scrape)
MIN_FIT_SAMPLES = 30    # minimum history before we fit any model
PROPHET_MIN_SAMPLES = 1000  # history needed before Prophet seasonality is credible


class ChannelForecaster:
    """
    Single-channel exponential-smoothing forecaster.
    Maintains a rolling history buffer, fits Holt-Winters on each call,
    and returns the t+N_HORIZON point estimate.
    """

    def __init__(self, channel_name: str, maxlen: int = 500):
        self.name = channel_name
        self._history: collections.deque = collections.deque(maxlen=maxlen)
        self._last_forecast: float | None = None

    def update(self, value: float):
        self._history.append(float(value))

    def forecast(self) -> float | None:
        """
        Returns the t+N_HORIZON point forecast, or None if not enough data.
        """
        n = len(self._history)
        if n < MIN_FIT_SAMPLES:
            return None

        series = np.array(self._history, dtype=float)

        # Try Prophet if we have enough history and it's installed
        if HAS_PROPHET and n >= PROPHET_MIN_SAMPLES:
            try:
                df = pd.DataFrame({"ds": pd.date_range("2000-01-01", periods=n, freq="s"),
                                   "y": series})
                m = Prophet(yearly_seasonality=False, weekly_seasonality=False,
                            daily_seasonality=True, interval_width=0.80)
                m.fit(df, algorithm="Newton")
                future = m.make_future_dataframe(periods=N_HORIZON, freq="s")
                forecast_df = m.predict(future)
                pred = float(forecast_df["yhat"].iloc[-1])
                self._last_forecast = pred
                return pred
            except Exception:
                pass  # fall through to Holt-Winters

        # Default: Holt-Winters double exponential smoothing
        if HAS_STATSMODELS:
            try:
                model = ExponentialSmoothing(
                    series,
                    trend="add",
                    seasonal=None,        # no seasonality claim on short data
                    initialization_method="estimated",
                )
                fit = model.fit(optimized=True, remove_bias=False)
                forecast = fit.forecast(N_HORIZON)
                pred = float(forecast[-1])
                self._last_forecast = pred
                return pred
            except Exception:
                pass

        # Fallback: simple linear extrapolation using last 10 points
        window = series[-10:]
        if len(window) >= 2:
            slope = (window[-1] - window[0]) / (len(window) - 1)
            pred = float(window[-1] + slope * N_HORIZON)
            self._last_forecast = pred
            return pred

        return None

    @property
    def current_value(self) -> float | None:
        return self._history[-1] if self._history else None

    @property
    def history_len(self) -> int:
        return len(self._history)


class TrendForecaster:
    """
    Multi-channel slow-trend forecaster.  One ChannelForecaster per metric.
    Designed to be polled once per scrape cycle by the Digital Twin.
    """

    def __init__(self):
        self._channels: dict[str, ChannelForecaster] = {}

    def update(self, channel: str, value: float):
        if channel not in self._channels:
            self._channels[channel] = ChannelForecaster(channel)
        self._channels[channel].update(value)

    def update_batch(self, metrics: dict[str, float]):
        for ch, val in metrics.items():
            self.update(ch, val)

    def forecast(self, channel: str) -> float | None:
        if channel not in self._channels:
            return None
        return self._channels[channel].forecast()

    def forecast_all(self) -> dict[str, float | None]:
        return {ch: fc.forecast() for ch, fc in self._channels.items()}

    def get_divergence(self, channel: str, actual: float) -> float | None:
        """
        Returns |forecast - actual| as an absolute divergence score,
        or None if no forecast is available.
        """
        pred = self.forecast(channel)
        if pred is None:
            return None
        return abs(pred - actual)

    def channels_ready(self) -> list[str]:
        """Channels that have enough history for a valid forecast."""
        return [ch for ch, fc in self._channels.items()
                if fc.history_len >= MIN_FIT_SAMPLES]

    def summary(self) -> str:
        lines = [f"TrendForecaster — {len(self._channels)} channels"]
        for ch, fc in list(self._channels.items())[:10]:
            pred = fc._last_forecast
            cur  = fc.current_value
            lines.append(
                f"  {ch[:40]:40s}  cur={cur:.4f}  t+{N_HORIZON}s={pred:.4f}"
                if pred is not None and cur is not None
                else f"  {ch[:40]:40s}  [warming up: {fc.history_len}/{MIN_FIT_SAMPLES}]"
            )
        if len(self._channels) > 10:
            lines.append(f"  ... and {len(self._channels)-10} more channels")
        return "\n".join(lines)


# ── Demo ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[*] TrendForecaster demo — simulating gradual latency creep\n")
    fc = TrendForecaster()

    np.random.seed(0)
    # 50 healthy samples then a gradual rise
    for i in range(80):
        latency = 5.0 + (i * 0.15 if i > 30 else 0) + np.random.normal(0, 0.3)
        loss    = 0.0 + (i * 0.01 if i > 50 else 0) + np.random.normal(0, 0.05)
        fc.update("rtt_ms_pe1_p1", latency)
        fc.update("loss_pct_pe1_p1", max(0, loss))

    pred_lat  = fc.forecast("rtt_ms_pe1_p1")
    pred_loss = fc.forecast("loss_pct_pe1_p1")
    print(f"  rtt_ms_pe1_p1   — current: {fc._channels['rtt_ms_pe1_p1'].current_value:.2f}ms  "
          f"t+{N_HORIZON}s forecast: {pred_lat:.2f}ms")
    print(f"  loss_pct_pe1_p1 — current: {fc._channels['loss_pct_pe1_p1'].current_value:.4f}%  "
          f"t+{N_HORIZON}s forecast: {pred_loss:.4f}%")

    divergence = fc.get_divergence("rtt_ms_pe1_p1", actual=6.0)
    print(f"\n  Divergence (forecast vs actual=6.0ms): {divergence:.3f}ms")
    print(f"\n{fc.summary()}")
