"""Correctness tests for plot-timeseries."""

from pathlib import Path

import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_figure_history


@pytest.fixture(scope="module")
def plot_timeseries():
    return load_skill("plot-timeseries", "plot_timeseries").plot_timeseries


def test_single_input_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "ts.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_fontsize_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "ts.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--fontsize",
        "22",
        "--title",
        "Large labels",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_reduce_spatial_dims(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "ts.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--title",
        "Area mean",
    )

    assert Path(out).exists()


def test_forecast_step_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast()
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "fc.zarr")
    out = tmp_path / "ts.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_two_inputs_write_png(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "ts.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_repeated_dash_i_keeps_every_input(tmp_path, plot_timeseries):
    # First file has no precip. If a second -i overwrote the first (nargs="+"),
    # only b.zarr would be plotted and this would succeed.
    a = write_zarr(make_gridded(name="other"), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(), tmp_path / "b.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_timeseries,
            "-i",
            str(a),
            "-i",
            str(b),
            "-o",
            str(tmp_path / "ts.png"),
            "-v",
            "precip",
            "--reduce",
            "latitude",
            "--reduce",
            "longitude",
        )
    assert exc.value.code == 2


def test_y_label_shows_units_from_pint():
    mod = load_skill("plot-timeseries", "plot_timeseries")
    da = make_gridded()["precip"]
    assert mod._y_label("precip", da) == "precip [mm/day]"
    assert mod._y_label("precip", da.pint.quantify()) == "precip [mm/day]"
    da.attrs["long_name"] = "IMERG daily precipitation"
    da.attrs["GRIB_name"] = "Precipitation rate"
    assert mod._y_label("precip", da) == "IMERG daily precipitation [mm/day]"


def test_trace_label_prefers_station_id_over_tahmo_source():
    import numpy as np
    import xarray as xr

    mod = load_skill("plot-timeseries", "plot_timeseries")
    ds = xr.Dataset(
        {"precip": (("time",), [1.0, 2.0])},
        coords={
            "time": np.array(["2026-08-19", "2026-08-20"], dtype="datetime64[ns]"),
            "station_id": "TA00072",
            "name": "Likoni",
        },
    )
    ds.attrs["weather_skills_source"] = "tahmo"
    assert mod._trace_label(ds, 0) == "TA00072 Likoni"


def test_trace_label_uses_filename_when_source_is_shared():
    import numpy as np
    import xarray as xr

    mod = load_skill("plot-timeseries", "plot_timeseries")
    ds = xr.Dataset(
        {"precip": (("time",), [1.0, 2.0])},
        coords={"time": np.array(["2026-08-19", "2026-08-20"], dtype="datetime64[ns]")},
    )
    ds.attrs["weather_skills_source"] = "tahmo"
    ds.encoding["source"] = "/tmp/ta00072.zarr"
    assert mod._trace_label(ds, 0) == "ta00072"


def test_trace_label_respects_explicit_override():
    mod = load_skill("plot-timeseries", "plot_timeseries")
    ds = make_gridded(n_time=2)
    assert mod._trace_label(ds, 0, "Custom name") == "Custom name"


def test_day_of_year_tick_label():
    mod = load_skill("plot-timeseries", "plot_timeseries")
    assert mod._day_of_year_tick_label(1) == "Jan 1"
    assert mod._day_of_year_tick_label(274) == "Oct 1"
    assert mod._day_of_year_tick_label(366) == "Dec 31"


def test_apply_day_of_year_ticks(tmp_path, plot_timeseries):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    mod = load_skill("plot-timeseries", "plot_timeseries")
    fig, ax = plt.subplots()
    ax.plot([1, 180, 274, 365], [1, 2, 3, 4])
    ax.set_xlim(1, 366)
    mod._apply_day_of_year_ticks(ax)
    fig.canvas.draw()
    labels = [tick.get_text() for tick in ax.get_xticklabels()]
    assert "Oct 1" in labels
    assert "Jan 1" in labels
    assert "274" not in labels
    plt.close(fig)

    src = write_zarr(make_gridded(n_time=12, start="2023-01-01"), tmp_path / "in.zarr")
    out = tmp_path / "doy.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--align-day-of-year",
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )
    assert out.exists()


def test_bar_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "bars.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--style",
        "bar",
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_bar_grouped_multi_input_writes_png(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "a.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "b.zarr")
    out = tmp_path / "grouped.png"

    run_skill(
        plot_timeseries,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--style",
        "bar",
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--title",
        "Grouped",
    )

    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]["skill"] == "plot-timeseries"
    assert history[-1]["args"]["style"] == "bar"


def test_bar_forecast_step_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast()
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "fc.zarr")
    out = tmp_path / "bars.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--style",
        "bar",
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_parse_trace_selector_and_aliases():
    import argparse

    mod = load_skill("plot-timeseries", "plot_timeseries")
    spec = mod.parse_trace("2026:color=black,lw=2.5,ms=7,zorder=5")
    assert spec.selector == "2026"
    assert spec.options == {
        "color": "black",
        "linewidth": 2.5,
        "markersize": 7.0,
        "zorder": 5.0,
    }
    assert str(spec) == "2026:color=black,lw=2.5,ms=7,zorder=5"
    styled = mod.parse_trace("clim:style=line,ls=--,lw=2.5")
    assert styled.options == {"style": "line", "linestyle": "--", "linewidth": 2.5}
    with pytest.raises(argparse.ArgumentTypeError, match="SELECTOR:k=v"):
        mod.parse_trace("black")
    with pytest.raises(argparse.ArgumentTypeError, match="unknown --trace option"):
        mod.parse_trace("1:colour=red")
    with pytest.raises(argparse.ArgumentTypeError, match="must be line or bar"):
        mod.parse_trace("1:style=scatter")


def test_resolve_trace_styles_star_then_token():
    mod = load_skill("plot-timeseries", "plot_timeseries")
    labels = ["chirps_2006", "chirps_2015", "chirps_2026"]
    styles = mod.resolve_trace_styles(
        labels,
        [
            mod.parse_trace("*:color=0.65,linewidth=1.2"),
            mod.parse_trace("2026:color=black,linewidth=2.5,zorder=5"),
        ],
    )
    assert styles[0] == {"color": "0.65", "linewidth": 1.2}
    assert styles[1] == {"color": "0.65", "linewidth": 1.2}
    assert styles[2] == {
        "color": "black",
        "linewidth": 2.5,
        "zorder": 5.0,
    }


def test_resolve_trace_styles_index_and_unmatched():
    from weather_skills_core.errors import UsageError

    mod = load_skill("plot-timeseries", "plot_timeseries")
    labels = ["a", "b"]
    styles = mod.resolve_trace_styles(labels, [mod.parse_trace("2:color=red")])
    assert styles[0] == {}
    assert styles[1] == {"color": "red"}
    with pytest.raises(UsageError, match="matched no series"):
        mod.resolve_trace_styles(labels, [mod.parse_trace("9:color=red")])
    with pytest.raises(UsageError, match="more than one series"):
        mod.resolve_trace_styles(
            ["yr_2026_a", "yr_2026_b"],
            [mod.parse_trace("2026:color=black")],
        )


def test_along_dim_resolves_member_alias():
    import numpy as np
    import xarray as xr

    mod = load_skill("plot-timeseries", "plot_timeseries")
    da = xr.DataArray(
        np.ones((3, 4)),
        dims=("number", "step"),
        coords={"number": [0, 1, 2], "step": np.arange(4)},
    )
    assert mod._along_dim(da, "number") == "number"
    assert mod._along_dim(da, "member") == "number"
    assert mod._along_dim(da, "realization") == "number"
    assert mod._along_dim(da, "time") is None


def test_draw_lines_along_is_one_call_one_legend_entry():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    mod = load_skill("plot-timeseries", "plot_timeseries")
    fig, ax = plt.subplots()
    y = np.column_stack([np.arange(4.0), np.arange(4.0) + 1.0, np.arange(4.0) + 2.0])
    series = [([1, 2, 3, 4], y, "ens")]
    mod._draw_lines(ax, series, [{}])
    assert len(ax.get_lines()) == 3
    colors = {line.get_color() for line in ax.get_lines()}
    assert len(colors) == 1
    handles, labels = mod._legend_handles(ax, series)
    assert labels == ["ens"]
    assert len(handles) == 1
    plt.close(fig)


def test_along_number_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast(members=5)
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "ens.zarr")
    out = tmp_path / "spaghetti.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--along",
        "number",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]["args"]["along"] == "number"


def test_along_member_alias_and_1d_overlay(tmp_path, plot_timeseries):
    ens = make_forecast(members=4, fill=1.0)
    ens["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    obs = make_gridded(n_time=3, fill=2.0, name="tp")
    obs["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    ens_path = write_zarr(ens, tmp_path / "ens.zarr")
    obs_path = write_zarr(obs, tmp_path / "obs.zarr")
    out = tmp_path / "overlay.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(ens_path),
        "-i",
        str(obs_path),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--along",
        "member",
        "--label",
        "ens",
        "--label",
        "obs",
        "--trace",
        "obs:color=black,linewidth=2.5,alpha=1",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_along_101_members_is_one_input(tmp_path, plot_timeseries):
    ds = make_forecast(n_step=6, lats=(1.0,), lons=(10.0,), members=101, fill=0.5)
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "ens101.zarr")
    out = tmp_path / "ens101.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(src),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--along",
        "number",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0


def test_leftover_member_dim_suggests_along(tmp_path, plot_timeseries):
    ds = make_forecast(members=3)
    ds["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "ens.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_timeseries,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "ts.png"),
            "--reduce",
            "latitude",
            "--reduce",
            "longitude",
        )
    assert exc.value.code == 2


def test_along_missing_dim_with_other_extras_exits(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_timeseries,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "ts.png"),
            "--along",
            "number",
        )
    assert exc.value.code == 2


def test_along_bar_overlay_writes_png(tmp_path, plot_timeseries):
    ens = make_forecast(members=3, fill=1.0)
    ens["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    obs = make_gridded(n_time=3, fill=2.0, name="tp")
    obs["tp"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    obs_path = write_zarr(obs, tmp_path / "obs.zarr")
    ens_path = write_zarr(ens, tmp_path / "ens.zarr")
    out = tmp_path / "bars_plus_ens.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(obs_path),
        "-i",
        str(ens_path),
        "-o",
        str(out),
        "--style",
        "bar",
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--along",
        "number",
        "--label",
        "obs",
        "--label",
        "ens",
    )
    assert Path(out).exists()


def test_draw_lines_applies_color_and_width():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.colors as mcolors
    import matplotlib.pyplot as plt

    mod = load_skill("plot-timeseries", "plot_timeseries")
    fig, ax = plt.subplots()
    series = [([1, 2], [0.0, 1.0], "chirps_2006"), ([1, 2], [1.0, 2.0], "chirps_2026")]
    styles = mod.resolve_trace_styles(
        ["chirps_2006", "chirps_2026"],
        [
            mod.parse_trace("*:color=0.65"),
            mod.parse_trace("2026:color=black,linewidth=3"),
        ],
    )
    mod._draw_lines(ax, series, styles)
    lines = ax.get_lines()
    assert mcolors.to_hex(lines[0].get_color()) == mcolors.to_hex("0.65")
    assert mcolors.to_hex(lines[1].get_color()) == "#000000"
    assert lines[1].get_linewidth() == 3
    plt.close(fig)


def test_trace_writes_png_and_stamps_args(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / "chirps_2006.zarr")
    b = write_zarr(make_gridded(fill=2.0), tmp_path / "chirps_2026.zarr")
    out = tmp_path / "analogs.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(a),
        "-i",
        str(b),
        "-o",
        str(out),
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--trace",
        "*:color=0.65,linewidth=1.2",
        "--trace",
        "2026:color=black,linewidth=2.5,zorder=5",
    )
    assert Path(out).exists()
    history = load_figure_history(out)
    assert history[-1]["args"]["trace"] == [
        "*:color=0.65,linewidth=1.2",
        "2026:color=black,linewidth=2.5,zorder=5",
    ]


def test_trace_bar_rejects_linewidth(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_timeseries,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "bars.png"),
            "--style",
            "bar",
            "--reduce",
            "latitude",
            "--reduce",
            "longitude",
            "--trace",
            "1:linewidth=3",
        )
    assert exc.value.code == 2


def test_draw_mixed_bars_and_line():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    mod = load_skill("plot-timeseries", "plot_timeseries")
    fig, ax = plt.subplots()
    series = [
        ([1.0, 2.0, 3.0], [1.0, 2.0, 1.5], "obs"),
        ([1.0, 2.0, 3.0], [0.8, 1.1, 0.9], "clim"),
    ]
    styles = mod.resolve_trace_styles(
        ["obs", "clim"],
        [mod.parse_trace("clim:style=line,linestyle=--,linewidth=2.5,marker=none")],
    )
    mod._draw_traces(ax, series, styles, "bar")
    assert len(ax.patches) == 3
    assert len(ax.get_lines()) == 1
    line = ax.get_lines()[0]
    assert line.get_linestyle() == "--"
    assert line.get_linewidth() == 2.5
    handles, labels = mod._legend_handles(ax, series)
    assert labels == ["obs", "clim"]
    assert handles[1] is line
    plt.close(fig)


def test_place_legend_below_axis():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    mod = load_skill("plot-timeseries", "plot_timeseries")
    fig, ax = plt.subplots()
    ax.plot([1, 2], [1, 2], label="a")
    ax.plot([1, 2], [2, 3], label="b")
    handles, labels = mod._legend_handles(ax, [([1, 2], [1, 2], "a"), ([1, 2], [2, 3], "b")])
    legend = mod._place_legend_below(ax, handles, labels, fontsize=12)
    anchor = legend.get_bbox_to_anchor()._bbox
    assert anchor.y0 < 0
    plt.close(fig)


def test_trace_per_series_style_bar_plus_line(tmp_path, plot_timeseries):
    obs = write_zarr(make_gridded(fill=1.0), tmp_path / "obs.zarr")
    clim = write_zarr(make_gridded(fill=0.5), tmp_path / "clim.zarr")
    out = tmp_path / "obs_vs_clim.png"
    run_skill(
        plot_timeseries,
        "-i",
        str(obs),
        "-i",
        str(clim),
        "-o",
        str(out),
        "--style",
        "bar",
        "--reduce",
        "latitude",
        "--reduce",
        "longitude",
        "--label",
        "obs",
        "--label",
        "clim",
        "--trace",
        "clim:style=line,linestyle=--,linewidth=2.5,marker=none",
    )
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]["args"]["style"] == "bar"
    assert history[-1]["args"]["trace"] == [
        "clim:style=line,linestyle=--,linewidth=2.5,marker=none"
    ]


def test_trace_unmatched_selector_exits(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_timeseries,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "ts.png"),
            "--reduce",
            "latitude",
            "--reduce",
            "longitude",
            "--trace",
            "2026:color=black",
        )
    assert exc.value.code == 2
