"""
Simple GEX Dashboard - Direct WebSocket approach (no background threads)
Fetches data once when you click "Fetch Data", then displays it
Works on weekends with Friday's closing data
"""
from typing import Any

import streamlit as st
import json
import time
from datetime import datetime, timedelta
from websocket import create_connection
import pandas as pd
import plotly.graph_objects as go

from gex_db import listDB, saveDB, getDB
from ibkr_connector import fetch_option_data
from utils.auth import ensure_streamer_token
from utils.gex_calculator import GEXCalculator

st.set_page_config(page_title="GEX Dashboard", page_icon="📊", layout="wide")

# Preset symbol configuration
PRESET_SYMBOLS = {
    "SPX": {"option_prefix": "SPXW", "default_price": 6000, "increment": 5},
    "NDX": {"option_prefix": "NDXP", "default_price": 20000, "increment": 25},
    "SPY": {"option_prefix": "SPY", "default_price": 680, "increment": 1},
    "QQQ": {"option_prefix": "QQQ", "default_price": 612, "increment": 1},
    "IWM": {"option_prefix": "IWM", "default_price": 240, "increment": 1},
    "DIA": {"option_prefix": "DIA", "default_price": 450, "increment": 1},
}

def aggregate_by_strike(option_data):
    """Aggregate volume and OI by strike from option data"""
    from utils.gex_calculator import parse_option_symbol

    strike_data = {}

    for symbol, data in option_data.items():
        parsed = parse_option_symbol(symbol)
        if not parsed:
            continue

        strike = parsed['strike']
        opt_type = parsed['type']

        if strike not in strike_data:
            strike_data[strike] = {
                'call_oi': 0,
                'put_oi': 0,
                'call_volume': 0,
                'put_volume': 0,
                'call_iv': None,
                'put_iv': None
            }

        # Convert to numbers (might be strings from WebSocket or NaN)
        import math

        try:
            oi = float(data.get('oi', 0) or 0)
            if math.isnan(oi):
                oi = 0
        except (ValueError, TypeError):
            oi = 0

        try:
            volume = float(data.get('volume', 0) or 0)
            if math.isnan(volume):
                volume = 0
        except (ValueError, TypeError):
            volume = 0

        iv = data.get('iv')

        if opt_type == 'C':
            strike_data[strike]['call_oi'] += int(oi)
            strike_data[strike]['call_volume'] += int(volume)
            if iv:
                strike_data[strike]['call_iv'] = iv
        else:
            strike_data[strike]['put_oi'] += int(oi)
            strike_data[strike]['put_volume'] += int(volume)
            if iv:
                strike_data[strike]['put_iv'] = iv

    # Convert to DataFrame
    rows = []
    for strike, data in strike_data.items():
        rows.append({
            'strike': strike,
            'call_oi': data['call_oi'],
            'put_oi': data['put_oi'],
            'call_volume': data['call_volume'],
            'put_volume': data['put_volume'],
            'total_oi': data['call_oi'] + data['put_oi'],
            'total_volume': data['call_volume'] + data['put_volume'],
            'call_iv': data['call_iv'],
            'put_iv': data['put_iv']
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values('strike').reset_index(drop=True)
    return df

def st_log(level, message):
    if level == "info":
        st.info(message)
    elif level == "warning":
        st.warning(message)
    elif level == "success":
        st.success(message)

def createCalculator(option_data: Any | None, price: int) -> GEXCalculator:
    calc = GEXCalculator()
    calc.update_spot_price(price)

    for symbol_name, data in option_data.items():
        if "gamma" in data and "oi" in data:
            gamma = data["gamma"]
            oi = data["oi"]
            if gamma is not None and oi is not None:
                calc.update_gamma(symbol_name, gamma, oi)
    return calc

def main():
    st.title("📊 Options Gamma Exposure Dashboard")

    # Initialize session state
    if 'data_fetched' not in st.session_state:
        st.session_state.data_fetched = False
    if 'gex_calculator' not in st.session_state:
        st.session_state.gex_calculator = None
    if 'auto_refresh' not in st.session_state:
        st.session_state.auto_refresh = False
    if 'last_fetch_time' not in st.session_state:
        st.session_state.last_fetch_time = 0
    if 'option_data' not in st.session_state:
        st.session_state.option_data = {}
    if 'gex_live' not in st.session_state:
        st.session_state.gex_live = "Live"
    if 'gex_view' not in st.session_state:
        st.session_state.gex_view = "Calls vs Puts"
    if 'volume_view' not in st.session_state:
        st.session_state.volume_view = "Calls vs Puts"

    # Sidebar controls
    with st.sidebar:
        st.header("⚙️ Settings")

        # Symbol selection: Preset or Custom
        symbol_mode = st.radio("Symbol Mode", ["Preset", "Custom"], horizontal=True)

        if symbol_mode == "Preset":
            symbol = st.selectbox("Underlying", list(PRESET_SYMBOLS.keys()))
            config = PRESET_SYMBOLS[symbol]
            option_prefix = config["option_prefix"]
            increment = config["increment"]
            default_price = config["default_price"]
        else:
            st.caption("Enter any symbol and its option parameters")
            symbol = st.text_input("Underlying Symbol", value="AAPL", max_chars=10).upper()
            option_prefix = st.text_input("Option Prefix", value="AAPL", max_chars=10,
                                         help="Usually same as underlying (e.g., AAPL, TSLA)").upper()
            increment = st.number_input("Strike Increment", min_value=0.5, max_value=100.0, value=2.5, step=0.5,
                                       help="SPY/QQQ: 1, AAPL: 2.5, TSLA: 5, SPX: 5, NDX: 25")
            default_price = st.number_input("Fallback Price", min_value=1.0, max_value=100000.0, value=100.0,
                                          help="Used if live price unavailable")

        # Default expiration to today's date
        default_exp = datetime.now().strftime("%y%m%d")

        expiration = st.text_input(
            "Expiration (YYMMDD)",
            value=default_exp,
            max_chars=6,
            help="Today's date shown by default. Change to any option expiration (e.g., 251219 for Dec 19, 2025)"
        )

        with st.expander("Strike Range"):
            strikes_up = st.number_input("Strikes above center", min_value=5, max_value=50, value=25)
            strikes_down = st.number_input("Strikes below center", min_value=5, max_value=50, value=25)

        st.divider()

        # Auto-refresh controls
        st.subheader("🔄 Auto-Refresh")
        st.session_state.auto_refresh = st.checkbox(
            "Enable auto-refresh",
            value=st.session_state.auto_refresh,
            help="Automatically refetch data every X seconds"
        )

        if st.session_state.auto_refresh:
            refresh_interval = st.slider(
                "Refresh interval (seconds)",
                min_value=30,
                max_value=300,
                value=300,
                step=10,
                help="How often to refresh data"
            )
        else:
            refresh_interval = 60

        st.divider()

        # Manual fetch button
        fetch_triggered = st.button("🔄 Fetch Data", type="primary", width='stretch')

        # Auto-fetch logic
        auto_triggered = False
        if st.session_state.auto_refresh:
            current_time = time.time()
            if current_time - st.session_state.last_fetch_time >= refresh_interval:
                fetch_triggered = True
                auto_triggered = True

        def gex_live_callback():
            if 'gex_live_radio' not in st.session_state or st.session_state['gex_live_radio'] == "Live":
                st.session_state.historical_data = None
                st.session_state.historical_pos = -1
            else:
                st.session_state.historical_data = listDB(datetime.strptime(expiration, "%y%m%d"))
                st.session_state.historical_pos = len(st.session_state.historical_data) - 1

                st.session_state.historical_gex_df = []
                st.session_state.historical_metrics = []
                st.session_state.historical_max_call_gex = 0
                st.session_state.historical_max_put_gex = 0
                st.session_state.historical_min_net_gex = float('inf')
                st.session_state.historical_max_net_gex = 0

                for (_, option) in st.session_state.historical_data:
                    calc = createCalculator(option, st.session_state.underlying_price)
                    gex_df = calc.get_gex_by_strike()
                    metrics = calc.get_total_gex_metrics()

                    st.session_state.historical_gex_df.append(gex_df)
                    st.session_state.historical_metrics.append(metrics)
                    st.session_state.historical_max_call_gex = max(st.session_state.historical_max_call_gex, gex_df['call_gex'].max())
                    st.session_state.historical_max_put_gex = max(st.session_state.historical_max_put_gex, gex_df['put_gex'].max())
                    st.session_state.historical_min_net_gex = min(st.session_state.historical_min_net_gex, gex_df['net_gex'].min())
                    st.session_state.historical_max_net_gex = max(st.session_state.historical_max_net_gex, gex_df['net_gex'].max())

        if fetch_triggered:
            with st.spinner(f"Fetching {symbol} data..."):
                try:
                    price, option_data = fetch_option_data(
                        st_log,
                        symbol,
                        option_prefix,
                        expiration,
                        strikes_up,
                        strikes_down
                    )

                    # TESTING: load test data
                    # price = 6800
                    # option_data = getDB(datetime(2026,2,14, 22,16,0))
                    # option_data = listDB(datetime(2026,2,14, 22,16,0))[0]

                    # Calculate GEX
                    calc = createCalculator(option_data, price)

                    if not auto_triggered:
                        gex_live_callback()
                    else:
                        saveDB(current_time, option_data)

                    # Store in session state
                    st.session_state.gex_calculator = calc
                    st.session_state.option_data = option_data
                    st.session_state.data_fetched = True
                    st.session_state.underlying_price = price
                    st.session_state.symbol = symbol
                    st.session_state.expiration = expiration
                    st.session_state.option_count = len(option_data)
                    st.session_state.last_fetch_time = time.time()

                    greeks_count = sum(1 for d in option_data.values() if "gamma" in d)
                    oi_count = sum(1 for d in option_data.values() if "oi" in d)
                    volume_count = sum(1 for d in option_data.values() if "volume" in d)

                    st.success(f"✅ Data fetched! Greeks: {greeks_count}, OI: {oi_count}")
                    st.rerun()

                except Exception as e:
                    st.error(f"❌ Error: {e}")
                    import traceback
                    st.code(traceback.format_exc())

        st.divider()

        if st.session_state.data_fetched:
            st.metric(f"{st.session_state.symbol} Price", f"${st.session_state.underlying_price:,.2f}")
            st.caption(f"Options: {st.session_state.option_count}")

            # Show last fetch time
            if st.session_state.last_fetch_time > 0:
                elapsed = time.time() - st.session_state.last_fetch_time
                st.caption(f"⏱️ Last fetch: {int(elapsed)}s ago")

                if st.session_state.auto_refresh:
                    next_refresh = max(0, refresh_interval - elapsed)

                    # Simple countdown display with color coding
                    if next_refresh <= 5:
                        # About to refresh - red warning
                        st.error(f"🔄 **REFRESHING IN {int(next_refresh)}s**")
                    elif next_refresh <= 15:
                        # Getting close - yellow warning
                        st.warning(f"🔄 Next refresh: **{int(next_refresh)}s**")
                    else:
                        # Plenty of time - green info
                        st.success(f"🔄 Next refresh: **{int(next_refresh)}s**")

                    # Progress bar showing time remaining
                    progress = (refresh_interval - elapsed) / refresh_interval
                    st.progress(max(0.0, min(1.0, progress)))

    # Main display
    if not st.session_state.data_fetched:
        st.info("👈 Configure settings and click 'Fetch Data' to load GEX data")
        st.caption("💡 Works on weekends! Shows Friday's closing data.")
        return

    def navigate(offset):
        if st.session_state.historical_pos <0:
            return

        if st.session_state.historical_pos + offset < 0:
            st.session_state.historical_pos = 0
        elif st.session_state.historical_pos + offset >= len(st.session_state.historical_data):
            st.session_state.historical_pos = len(st.session_state.historical_data) - 1
        else:
            st.session_state.historical_pos += offset

    col1, col2, col3, col4, col5, col6, col7, col8 = st.columns([2,0.1,0.4,0.3,1.5,0.3,0.4,0.5])
    with col1:
        gex_live = st.radio(
            "GEX Live",
            ["Live", "Historical"],
            index = ["Live", "Historical"].index(st.session_state.gex_live),
            key="gex_live_radio",
            horizontal=True,
            help="Live vs Historical",
            on_change=gex_live_callback
        )
        st.session_state.gex_live = gex_live
    with col3:
        if st.session_state.gex_live == "Historical":
            if st.button("<<", type="secondary"):
                navigate(-5)
    with col4:
        if st.session_state.gex_live == "Historical":
            if st.button("<", type="secondary"):
                navigate(-1)
    with col6:
        if st.session_state.gex_live == "Historical":
            if st.button("\>", type="secondary"):
                navigate(1)
    with col7:
        if st.session_state.gex_live == "Historical":
            if st.button("\>\>", type="secondary"):
                navigate(5)
    with col5:
        if st.session_state.gex_live == "Historical":
            data = st.session_state.historical_data
            pos = st.session_state.historical_pos
            if pos >= 0:
                st.subheader(f"{data[pos][0]} {pos+1} of {len(data)}")
            else:
                st.subheader("no record")

    # Get GEX data
    calc = st.session_state.gex_calculator
    gex_df, metrics = pd.DataFrame(), {}
    if st.session_state.gex_live == "Historical":
        data = st.session_state.historical_data
        pos = st.session_state.historical_pos
        print(f"Historical pos: {pos}")

        if pos >= 0:
            gex_df = st.session_state.historical_gex_df[pos]
            metrics = st.session_state.historical_metrics[pos]
    else:
        gex_df = calc.get_gex_by_strike()
        metrics = calc.get_total_gex_metrics()

    if gex_df.empty:
        st.warning("⚠️ No GEX data available. Try fetching again or check different expiration.")
        return

    # Display
    col1, col2 = st.columns([2, 1])

    with col1:
        # GEX View Selector
        gex_view = st.radio(
            "GEX View",
            ["Calls vs Puts", "Net GEX", "Absolute GEX"],
            index=["Calls vs Puts", "Net GEX", "Absolute GEX"].index(st.session_state.gex_view),
            key="gex_view_radio",
            horizontal=True,
            help="Calls vs Puts: Separate bars | Net GEX: Call-Put | Absolute GEX: |Net| magnitude"
        )
        st.session_state.gex_view = gex_view

        # Create chart based on selected view
        fig = go.Figure()

        if gex_view == "Calls vs Puts":
            # Original view: Calls (green) vs Puts (red)
            fig.add_trace(go.Bar(
                x=gex_df['strike'],
                y=gex_df['call_gex'],
                name='Call GEX',
                marker_color='green'
            ))
            fig.add_trace(go.Bar(
                x=gex_df['strike'],
                y=-gex_df['put_gex'],
                name='Put GEX',
                marker_color='red'
            ))
            barmode = 'relative'
            yaxis_title = 'Gamma Exposure ($)'

        elif gex_view == "Net GEX":
            # Net GEX: Call - Put (can be positive or negative)
            colors = ['green' if x >= 0 else 'red' for x in gex_df['net_gex']]
            fig.add_trace(go.Bar(
                x=gex_df['strike'],
                y=gex_df['net_gex'],
                name='Net GEX',
                marker_color=colors
            ))
            barmode = 'group'
            yaxis_title = 'Net GEX ($) - Green=Call Heavy, Red=Put Heavy'

        else:  # Absolute GEX
            # Absolute Net GEX: |Call - Put| (always positive)
            abs_gex = abs(gex_df['net_gex'])
            fig.add_trace(go.Bar(
                x=gex_df['strike'],
                y=abs_gex,
                name='|Net GEX|',
                marker_color='blue'
            ))
            barmode = 'group'
            yaxis_title = 'Absolute Net GEX ($)'

        # Add vertical line at underlying price
        fig.add_vline(
            x=st.session_state.underlying_price,
            line_dash="dash",
            line_color="orange",
            line_width=2,
            annotation_text=f"${st.session_state.underlying_price:,.2f}",
            annotation_position="top"
        )

        # Add vertical line at Zero Gamma level (Gamma Flip)
        if metrics.get('zero_gamma'):
            zero_gamma = metrics['zero_gamma']
            fig.add_vline(
                x=zero_gamma,
                line_dash="dot",
                line_color="purple",
                line_width=2,
                annotation_text=f"Zero Γ: ${zero_gamma:,.2f}",
                annotation_position="bottom"
            )

        # Format expiration for display
        exp_display = st.session_state.expiration
        try:
            exp_date = datetime.strptime(st.session_state.expiration, "%y%m%d")
            exp_display = exp_date.strftime("%b %d, %Y")
        except:
            pass

        fig.update_layout(
            title=f'{st.session_state.symbol} Gamma Exposure by Strike - {gex_view} (Exp: {exp_display})',
            xaxis_title='Strike Price',
            yaxis_title=yaxis_title,
            barmode=barmode,
            template='plotly_white',
            height=500
        )

        if st.session_state.gex_live == "Historical":
            if gex_view == "Calls vs Puts":
                fig.update_yaxes(range=[-st.session_state.historical_max_put_gex, st.session_state.historical_max_call_gex])
            elif gex_view == "Net GEX":
                fig.update_yaxes(range=[st.session_state.historical_min_net_gex, st.session_state.historical_max_net_gex])
            else:
                fig.update_yaxes(range=[0,max(-st.session_state.historical_min_net_gex, st.session_state.historical_max_net_gex)])

        st.plotly_chart(fig, width='stretch')

    with col2:
        st.subheader("📈 Total GEX")

        st.metric("Total Call GEX", f"${metrics['total_call_gex']:,.0f}")
        st.metric("Total Put GEX", f"${metrics['total_put_gex']:,.0f}")
        st.metric("Net GEX", f"${metrics['net_gex']:,.0f}")

        if metrics['max_gex_strike']:
            st.divider()
            st.metric("Max GEX Strike", f"${metrics['max_gex_strike']:,.0f}")

        if metrics.get('zero_gamma'):
            st.divider()
            zero_gamma = metrics['zero_gamma']
            st.metric(
                "Zero Gamma (Flip)",
                f"${zero_gamma:,.2f}",
                help="Strike where Net GEX crosses zero. Dealers long gamma above, short gamma below."
            )

    # Volume and Open Interest Section
    # Aggregate data by strike (used for IV Skew and Volume/OI)
    strike_df = None
    if st.session_state.gex_live == "Historical":
        data = st.session_state.historical_data
        pos = st.session_state.historical_pos
        strike_df = aggregate_by_strike(data[pos][1])
    else:
        strike_df = aggregate_by_strike(st.session_state.option_data)

    # IV Skew Section
    if not strike_df.empty and (strike_df['call_iv'].notna().any() or strike_df['put_iv'].notna().any()):
        st.divider()
        st.header("📈 Implied Volatility Skew")

        fig_iv = go.Figure()

        # Plot Call IV
        call_iv_data = strike_df[strike_df['call_iv'].notna()]
        if not call_iv_data.empty:
            fig_iv.add_trace(go.Scatter(
                x=call_iv_data['strike'],
                y=call_iv_data['call_iv'] * 100,  # Convert to percentage
                mode='lines+markers',
                name='Call IV',
                line=dict(color='green', width=2),
                marker=dict(size=6)
            ))

        # Plot Put IV
        put_iv_data = strike_df[strike_df['put_iv'].notna()]
        if not put_iv_data.empty:
            fig_iv.add_trace(go.Scatter(
                x=put_iv_data['strike'],
                y=put_iv_data['put_iv'] * 100,  # Convert to percentage
                mode='lines+markers',
                name='Put IV',
                line=dict(color='red', width=2),
                marker=dict(size=6)
            ))

        # Add vertical line at underlying price
        fig_iv.add_vline(
            x=st.session_state.underlying_price,
            line_dash="dash",
            line_color="orange",
            line_width=2,
            annotation_text=f"${st.session_state.underlying_price:,.2f}",
            annotation_position="top"
        )

        # Format expiration date for display (YYMMDD -> Mon DD, YYYY)
        exp_display = st.session_state.expiration
        try:
            exp_date = datetime.strptime(st.session_state.expiration, "%y%m%d")
            exp_display = exp_date.strftime("%b %d, %Y")
        except:
            pass

        fig_iv.update_layout(
            title=f'{st.session_state.symbol} Implied Volatility Skew - Exp: {exp_display}',
            xaxis_title='Strike Price',
            yaxis_title='Implied Volatility (%)',
            template='plotly_white',
            height=400,
            hovermode='x unified'
        )

        st.plotly_chart(fig_iv, width='stretch')

    st.divider()
    st.header("📊 Volume & Open Interest Analysis")

    if not strike_df.empty:
        # Two columns for OI and Volume charts
        col3, col4 = st.columns(2)

        with col3:
            # Open Interest Chart
            fig_oi = go.Figure()
            fig_oi.add_trace(go.Bar(
                x=strike_df['strike'],
                y=strike_df['call_oi'],
                name='Call OI',
                marker_color='green'
            ))
            fig_oi.add_trace(go.Bar(
                x=strike_df['strike'],
                y=-strike_df['put_oi'],
                name='Put OI',
                marker_color='red'
            ))

            # Add vertical line at underlying price
            fig_oi.add_vline(
                x=st.session_state.underlying_price,
                line_dash="dash",
                line_color="orange",
                line_width=2,
                annotation_text=f"${st.session_state.underlying_price:,.2f}",
                annotation_position="top"
            )

            fig_oi.update_layout(
                title='Open Interest by Strike',
                xaxis_title='Strike',
                yaxis_title='Open Interest',
                barmode='relative',
                template='plotly_white',
                height=400
            )
            st.plotly_chart(fig_oi, width='stretch')

        with col4:
            # Volume Chart with toggle
            volume_view = st.radio(
                "Volume View",
                ["Calls vs Puts", "Total Volume"],
                index=["Calls vs Puts", "Total Volume"].index(st.session_state.volume_view),
                key="volume_view_radio",
                horizontal=True,
                help="Switch between separate call/put volume or total volume by strike"
            )
            st.session_state.volume_view = volume_view

            fig_vol = go.Figure()

            if volume_view == "Calls vs Puts":
                # Separate calls and puts
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=strike_df['call_volume'],
                    name='Call Volume',
                    marker_color='lightgreen'
                ))
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=-strike_df['put_volume'],
                    name='Put Volume',
                    marker_color='lightcoral'
                ))
                barmode = 'relative'
            else:  # Total Volume
                # Total volume (calls + puts)
                total_volume = strike_df['call_volume'] + strike_df['put_volume']
                fig_vol.add_trace(go.Bar(
                    x=strike_df['strike'],
                    y=total_volume,
                    name='Total Volume',
                    marker_color='purple'
                ))
                barmode = 'group'

            # Add vertical line at underlying price
            fig_vol.add_vline(
                x=st.session_state.underlying_price,
                line_dash="dash",
                line_color="orange",
                line_width=2,
                annotation_text=f"${st.session_state.underlying_price:,.2f}",
                annotation_position="top"
            )

            fig_vol.update_layout(
                title=f'Volume by Strike - {volume_view}',
                xaxis_title='Strike',
                yaxis_title='Volume',
                barmode=barmode,
                template='plotly_white',
                height=400
            )
            st.plotly_chart(fig_vol, width='stretch')

        # Top Strikes Table
        st.subheader("🔝 Top Strikes")

        # Create tabs for different views
        tab1, tab2, tab3 = st.tabs(["By Total OI", "By Total Volume", "By Put/Call Ratio"])

        with tab1:
            top_oi = strike_df.nlargest(10, 'total_oi')[['strike', 'call_oi', 'put_oi', 'total_oi']]
            top_oi['strike'] = top_oi['strike'].apply(lambda x: f"${x:,.0f}")
            top_oi.columns = ['Strike', 'Call OI', 'Put OI', 'Total OI']
            st.dataframe(top_oi, hide_index=True, width='stretch')

        with tab2:
            top_vol = strike_df.nlargest(10, 'total_volume')[['strike', 'call_volume', 'put_volume', 'total_volume']]
            top_vol['strike'] = top_vol['strike'].apply(lambda x: f"${x:,.0f}")
            top_vol.columns = ['Strike', 'Call Vol', 'Put Vol', 'Total Vol']
            st.dataframe(top_vol, hide_index=True, width='stretch')

        with tab3:
            # Calculate put/call ratio
            pc_ratio_df = strike_df.copy()
            pc_ratio_df['pc_ratio_oi'] = pc_ratio_df['put_oi'] / pc_ratio_df['call_oi'].replace(0, 1)
            pc_ratio_df['pc_ratio_vol'] = pc_ratio_df['put_volume'] / pc_ratio_df['call_volume'].replace(0, 1)
            top_pc = pc_ratio_df.nlargest(10, 'pc_ratio_oi')[['strike', 'pc_ratio_oi', 'pc_ratio_vol', 'total_oi']]
            top_pc['strike'] = top_pc['strike'].apply(lambda x: f"${x:,.0f}")
            top_pc['pc_ratio_oi'] = top_pc['pc_ratio_oi'].apply(lambda x: f"{x:.2f}")
            top_pc['pc_ratio_vol'] = top_pc['pc_ratio_vol'].apply(lambda x: f"{x:.2f}")
            top_pc.columns = ['Strike', 'P/C Ratio (OI)', 'P/C Ratio (Vol)', 'Total OI']
            st.dataframe(top_pc, hide_index=True, width='stretch')

    # Auto-refresh logic
    if st.session_state.auto_refresh:
        time.sleep(1)  # Small delay before rerun
        st.rerun()


if __name__ == "__main__":
    main()
