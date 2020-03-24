# -*- coding: utf-8 -*-
import time
import os
import json
import gzip
import shutil
import requests
import numpy as np
import datashader as ds
import datashader.transfer_functions as tf

import dash
import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Input, Output
import dash_daq as daq
from plotly.colors import sequential
from pyproj import Transformer

from dask import delayed
from distributed import Client
from dask_cuda import LocalCUDACluster
import plotly.graph_objects as go
import plotly.express as px

import cudf
import pandas as pd
import cupy

# Disable cupy memory pool so that cupy immediately releases GPU memory
cupy.cuda.set_allocator(None)

# Colors
bgcolor = "#191a1a"  # mapbox dark map land color
text_color = "#cfd8dc"  # Material blue-grey 100
mapbox_land_color = "#343332"

# Figure template
row_heights = [80, 460, 200]
template = {
    'layout': {
        'paper_bgcolor': bgcolor,
        'plot_bgcolor': bgcolor,
        'font': {'color': text_color},
        "margin": {"r": 0, "t": 30, "l": 0, "b": 20},
        'bargap': 0.05,
        'xaxis': {'showgrid': False, 'automargin': True},
        'yaxis': {'showgrid': True, 'automargin': True},
    }
}


colors = {}
mappings = {}
colors['sex'] = ['#0000FF', '#00ff00']

# Load mapbox token from environment variable or file
token = os.getenv('MAPBOX_TOKEN')
if not token:
    token = open(".mapbox_token").read()


# Names of float columns
float_columns = [
    'x', 'y', 'age', 'sex'
]

column_labels = {
    'sex': 'sex',
}

mappings['sex'] = {
    -1: "All genders",
    0: 'Males',
    1: 'Females'
}

data_center_3857, data_3857, data_4326, data_center_4326 = [], [], [], []

def load_dataset(path):
    """
    Args:
        path: Path to arrow file containing mortgage dataset

    Returns:
        pandas DataFrame
    """
    df_d = cudf.read_parquet(path)
    df_d.sex = df_d.sex.to_pandas().astype('category')
    return df_d

def load_covid(url):
    df = pd.DataFrame(requests.get(url).json())
    valid_dates = df.groupby('dateChecked').count().query('state == 56').index.tolist()
    valid_df = df.query('dateChecked in @valid_dates').fillna(0, inplace=False)

    df2 = valid_df.assign(count_val = valid_df.positive.astype(str) + ', ' + \
      (valid_df.positive + valid_df.negative).astype(str) + ', ' + valid_df.death.astype(str))

    df2.drop(['positive', 'negative', 'pending', 'death'], axis=1, inplace=True)

    df2['category'] = "positive tests,total tests,death"

    df3 = pd.DataFrame({
        'date': np.repeat(df2.dateChecked, 3),
        'state': np.repeat(df2.state, 3),
        'category': df2.category.str.split(',', expand=True).values.ravel(),
        'cases':df2.count_val.str.split(',', expand=True).values.ravel()
    }).sort_values(['state', 'date'])

    return df3


def load_hospitals(path):
    df_hospitals = pd.read_csv(path, usecols=['X', 'Y', 'BEDS', 'NAME'])
    df_hospitals['BEDS_label']= df_hospitals.BEDS.replace(-999, 'unknown')
    df_hospitals.BEDS = df_hospitals.BEDS.replace(-999, 0)
    return df_hospitals

def set_projection_bounds(df_d):
    transformer_4326_to_3857 = Transformer.from_crs("epsg:4326", "epsg:3857")
    def epsg_4326_to_3857(coords):
        return [transformer_4326_to_3857.transform(*reversed(row)) for row in coords]

    transformer_3857_to_4326 = Transformer.from_crs("epsg:3857", "epsg:4326")
    def epsg_3857_to_4326(coords):
        return [list(reversed(transformer_3857_to_4326.transform(*row))) for row in coords]
    
    data_3857 = (
        [df_d.x.min(), df_d.y.min()],
        [df_d.x.max(), df_d.y.max()]
    )
    data_center_3857 = [[
        (data_3857[0][0] + data_3857[1][0]) / 2.0,
        (data_3857[0][1] + data_3857[1][1]) / 2.0,
    ]]

    data_4326 = epsg_3857_to_4326(data_3857)
    data_center_4326 = epsg_3857_to_4326(data_center_3857)

    return data_3857, data_center_3857, data_4326, data_center_4326

# Build Dash app and initial layout
def blank_fig(height):
    """
    Build blank figure with the requested height
    Args:
        height: height of blank figure in pixels
    Returns:
        Figure dict
    """
    return {
        'data': [],
        'layout': {
            'height': height,
            'template': template,
            'xaxis': {'visible': False},
            'yaxis': {'visible': False},
        }
    }


app = dash.Dash(__name__)
app.layout = html.Div(children=[
    html.Div([
        html.H1(children=[
            'Census 2010, Hospitals & Covid-19 Cases',
            html.A(
                html.Img(
                    src="https://camo.githubusercontent.com/38ca5c5f7d6afc09f8d50fd88abd4b212f0a6375/68747470733a2f2f7261706964732e61692f6173736574732f696d616765732f7261706964735f6c6f676f2e706e67",
                    style={'float': 'right', 'height': '50px', 'margin-right': '2%'}
                ), href="https://rapids.ai/"),
            html.A(
                html.Img(
                    src="assets/dash-logo.png",
                    style={'float': 'right', 'height': '50px', 'margin-right': '2%'}
                ), href="https://dash.plot.ly/"),

        ], style={'text-align': 'left'}),
    ]),
    html.Div(children=[
        html.Div(children=[
            html.Div(children=[
                html.Div(children=[
                    html.Button(
                        "Clear All Selections", id='clear-all', className='reset-button'
                    ),
                ]),
                html.H4([
                    "Selected Polpulation",
                ], className="container_title"),
                dcc.Loading(
                    dcc.Graph(
                        id='indicator-graph',
                        figure=blank_fig(row_heights[0]),
                        config={'displayModeBar': False},
                    ),
                    style={'height': row_heights[0]},
                )
            ], className='six columns pretty_container', id="indicator-div"),
            html.Div(children=[
                html.Div(children=[
                    html.Button(
                        "Reset GPU", id='reset-gpu', className='reset-button'
                    ),
                ]),
                html.H4([
                    "Configuration",
                ], className="container_title"),
                html.Table([
                    html.Col(style={'width': '100px'}),
                    html.Col(),
                    html.Col(),
                    html.Tr([
                            html.Td(
                                html.Div("Hospitals"), className="config-label"
                            ),
                            html.Td(daq.DarkThemeProvider(daq.BooleanSwitch(
                                on=True,
                                color='#00cc96',
                                id='hospitals-toggle',
                            ))),
                            html.Td(
                                html.Div("Color scale"), className="config-label"
                            ),
                            html.Td(dcc.Dropdown(
                                id='colorscale-dropdown',
                                options=[
                                    {'label': cs, 'value': cs}
                                    for cs in ['Viridis', 'Cividis', 'Inferno', 'Magma', 'Plasma']
                                ],
                                value='Viridis',
                                searchable=False,
                                clearable=False,
                            ), style={'width': '50%'}),
                            html.Div(id='reset-gpu-complete', style={'display': 'hidden'})
                        ])
                ], style={'width': '100%', 'height': row_heights[0]}),
            ], className='six columns pretty_container', id="config-div"),
        ]),
        html.Div(children=[
            html.Button("Clear Selection", id='reset-map', className='reset-button'),
            html.H4([
                "US Population(each individual)",
            ], className="container_title"),
            dcc.Graph(
                id='map-graph',
                config={'displayModeBar': False},
                figure=blank_fig(row_heights[1]),
            ),
        ], className='twelve columns pretty_container',
            style={
                'width': '98%',
                'margin-right': '0',
            },
            id="map-div"
        ),
        html.Div(children=[
            html.Div(
                children=[
                    html.Button(
                        "Clear Selection", id='clear-age', className='reset-button'
                    ),
                    html.H4([
                        "Age",
                    ], className="container_title"),
                    
                    dcc.Graph(
                        id='age-histogram',
                        config={'displayModeBar': False},
                        figure=blank_fig(row_heights[1]),
                        animate=True
                    ),
                ],
                className='twelve columns pretty_container', id="age-div"
            )
        ]),
        html.Div(children=[
            html.Div(
                children=[
                    html.H4([
                        "Covid-19 Cases",
                    ], className="container_title"),
                    
                    dcc.Graph(
                        id='covid-histogram',
                        config={'displayModeBar': False},
                        figure=blank_fig(row_heights[1]),
                        animate=True
                    ),
                ],
                className='twelve columns pretty_container', id="covid-div"
            )
        ])
    ]),
    html.Div(
        [
            html.H4('Acknowledgements', style={"margin-top": "0"}),
            dcc.Markdown('''\
 - Dashboard written in Python using the [Dash](https://dash.plot.ly/) web framework.
 - GPU accelerated provided by the [cudf](https://github.com/rapidsai/cudf) and
 [cupy](https://cupy.chainer.org/) libraries.
 - Base map layer is the ["dark" map style](https://www.mapbox.com/maps/light-dark/)
 provided by [mapbox](https://www.mapbox.com/).
 - Covid data: https://covidtracking.com/api/
 - Hospitals data: https://hifld-geoplatform.opendata.arcgis.com/datasets/hospitals
'''),
        ],
        style={
            'width': '98%',
            'margin-right': '0',
            'padding': '10px',
        },
        className='twelve columns pretty_container',
    ),
])

# Clear/reset button callbacks
@app.callback(
    Output('map-graph', 'relayoutData'),
    [Input('reset-map', 'n_clicks'), Input('clear-all', 'n_clicks')]
)
def clear_map(*args):
    return None

@app.callback(
    Output('age-histogram', 'selectedData'),
    [Input('clear-age', 'n_clicks'), Input('clear-all', 'n_clicks')]
)
def clear_age_hist_selections(*args):
    return None

# Query string helpers
def bar_selection_to_query(selection, column):
    """
    Compute pandas query expression string for selection callback data

    Args:
        selection: selectedData dictionary from Dash callback on a bar trace
        column: Name of the column that the selected bar chart is based on

    Returns:
        String containing a query expression compatible with DataFrame.query. This
        expression will filter the input DataFrame to contain only those rows that
        are contained in the selection.
    """
    point_inds = [p['label'] for p in selection['points']]
    xmin = min(point_inds) #bin_edges[min(point_inds)]
    xmax = max(point_inds) + 1 #bin_edges[max(point_inds) + 1]
    xmin_op = "<="
    xmax_op = "<="
    return f"{xmin} {xmin_op} {column} and {column} {xmax_op} {xmax}"


def build_query(selections, exclude=None):
    """
    Build pandas query expression string for cross-filtered plot

    Args:
        selections: Dictionary from column name to query expression
        exclude: If specified, column to exclude from combined expression

    Returns:
        String containing a query expression compatible with DataFrame.query.
    """
    other_selected = {sel for c, sel in selections.items() if (c != exclude and sel != -1)}
    if other_selected:
        return ' and '.join(other_selected)
    else:
        return None


# Plot functions
def build_colorscale(colorscale_name, transform, agg, agg_col):
    """
    Build plotly colorscale

    Args:
        colorscale_name: Name of a colorscale from the plotly.colors.sequential module
        transform: Transform to apply to colors scale. One of 'linear', 'sqrt', 'cbrt',
        or 'log'

    Returns:
        Plotly color scale list
    """
    global colors, mappings


    colors_temp = getattr(sequential, colorscale_name)
    if transform == "linear":
        scale_values = np.linspace(0, 1, len(colors_temp))
    elif transform == "sqrt":
        scale_values = np.linspace(0, 1, len(colors_temp)) ** 2
    elif transform == "cbrt":
        scale_values = np.linspace(0, 1, len(colors_temp)) ** 3
    elif transform == "log":
        scale_values = (10 ** np.linspace(0, 1, len(colors_temp)) - 1) / 9
    else:
        raise ValueError("Unexpected colorscale transform")
    return [(v, clr) for v, clr in zip(scale_values, colors_temp)]


def build_datashader_plot(
        df, aggregate, aggregate_column, colorscale_name, colorscale_transform,
        new_coordinates, position, x_range, y_range, df_hospitals
):
    """
    Build choropleth figure

    Args:
        df: pandas or cudf DataFrame
        aggregate: Aggregate operation (count, mean, etc.)
        aggregate_column: Column to perform aggregate on. Ignored for 'count' aggregate
        colorscale_name: Name of plotly colorscale
        colorscale_transform: Colorscale transformation
        clear_selection: If true, clear choropleth selection. Otherwise leave
            selection unchanged

    Returns:
        Choropleth figure dictionary
    """

    global data_3857, data_center_3857, data_4326, data_center_4326

    x0, x1 = x_range
    y0, y1 = y_range

    # Build query expressions
    query_expr_xy = f"(x >= {x0}) & (x <= {x1}) & (y >= {y0}) & (y <= {y1})"
    datashader_color_scale = {}

    if aggregate == 'count_cat':
        datashader_color_scale['color_key'] = colors[aggregate_column] 
    else:
        datashader_color_scale['cmap'] = [i[1] for i in build_colorscale(colorscale_name, colorscale_transform, aggregate, aggregate_column)]
        if not isinstance(df, cudf.DataFrame):
            df[aggregate_column] = df[aggregate_column].astype('int8')

    cvs = ds.Canvas(
        plot_width=1400,
        plot_height=1400,
        x_range=x_range, y_range=y_range
    )
    agg = cvs.points(
        df, x='x', y='y', agg=getattr(ds, aggregate)(aggregate_column)
    )

    cmin = cupy.asnumpy(agg.min().data)
    cmax = cupy.asnumpy(agg.max().data)
    # Count the number of selected people
    temp = agg.sum()
    temp.data = cupy.asnumpy(temp.data)
    n_selected = int(temp)

    if n_selected == 0:
        # Nothing to display
        lat = [None]
        lon = [None]
        customdata = [None]
        marker = {}
        layers = []
    else:
        # Shade aggregation into an image that we can add to the map as a mapbox
        # image layer
        max_px = 1
        img = tf.shade(agg, **datashader_color_scale)
        img = tf.dynspread(
                    img,
                    threshold=0.1,
                    max_px=max_px,
                    shape='circle',
                ).to_pil()

        # Add image as mapbox image layer. Note that as of version 4.4, plotly will
        # automatically convert the PIL image object into a base64 encoded png string
        layers = [
            {
                "sourcetype": "image",
                "source": img,
                "coordinates": new_coordinates
            }
        ]

        # Do not display any mapbox markers
        lat = [None]
        lon = [None]
        customdata = [None]
        marker = {}

    # Build map figure
    map_graph = {
        'data': [
        {
            'type': 'scattermapbox',
            'lat': lat, 'lon': lon,
            'customdata': customdata,
            'marker': dict(
                size=0,
                showscale=True,
                colorscale=build_colorscale(colorscale_name, colorscale_transform, aggregate, aggregate_column),
                cmin=cmin,
                cmax=cmax
            ),
            'hoverinfo': 'none'
        },],
        'layout': {
            'template': template,
            'uirevision': True,
            'mapbox': {
                'style': "dark",
                'accesstoken': token,
                'layers': layers,
            },
            'margin': {"r": 0, "t": 0, "l": 0, "b": 0},
            'height': 500,
            'shapes': [{
                'type': 'rect',
                'xref': 'paper',
                'yref': 'paper',
                'x0': 0,
                'y0': 0,
                'x1': 1,
                'y1': 1,
                'line': {
                    'width': 1,
                    'color': '#191a1a',
                }
            }],
            'showlegend':False
        },
    }

    if df_hospitals is not None and isinstance(df_hospitals, pd.DataFrame):
        map_graph['data'] = map_graph['data'] + [
             {
                'type': 'scattermapbox',
                'lat': df_hospitals.Y.values,
                'lon': df_hospitals.X.values,
                'marker': go.scattermapbox.Marker(
                    size=df_hospitals.BEDS.values + 250,
                    color='black',
                    sizeref=70,
                ),
                'hoverinfo': 'none'
            },
            {
                'type': 'scattermapbox',
                'lat': df_hospitals.Y.values,
                'lon': df_hospitals.X.values,
                'marker': go.scattermapbox.Marker(
                    size=df_hospitals.BEDS.values,
                    color='#f8f8f8',
                    opacity=1,
                    sizeref=70,
                ),
                'hovertemplate': (
                    '<b>%{hovertext}</b><br><br>BEDS = %{text}<extra></extra>'
                ),
                'text': df_hospitals.BEDS_label,
                'hovertext': df_hospitals.NAME,
                'mode': 'markers',
                'showlegend': False,
                'subplot': 'mapbox'
            }
        ]

    map_graph['layout']['mapbox'].update(position)

    return map_graph


def build_histogram_default_bins(
        df, column, selections, query_cache, orientation
):
    """
    Build histogram figure

    Args:
        df: pandas or cudf DataFrame
        column: Column name to build histogram from
        selections: Dictionary from column names to query expressions
        query_cache: Dict from query expression to filtered DataFrames

    Returns:
        Histogram figure dictionary
    """
    bin_edges = df.index.values
    counts = df.values

    range_ages = [20, 40, 60, 84, 85]
    colors_ages = ['#C700E5','#9D00DB', '#7300D2','#4900C8','#1F00BF']
    labels = ['0-20', '21-40', '41-60', '60-84', '85+']
    # centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    if orientation == 'h':
        fig = {
            'data':[],
            'layout': {
                'xaxis': {
                    'type': 'log',
                    'range': [0, 8],  # Up to 100M
                    'title': {
                        'text': "Count"
                    }
                },
                'selectdirection': 'v',
                'dragmode': 'select',
                'template': template,
                'uirevision': True,
            }
        }
    else:
        fig = {
            'data':[],
            'layout': {
                'yaxis': {
                    'type': 'log',
                    'range': [0, 8],  # Up to 100M
                    'title': {
                        'text': "Count"
                    }
                },
                'selectdirection': 'h',
                'dragmode': 'select',
                'template': template,
                'uirevision': True,
            }
        }

    for index, ages in enumerate(range_ages):
        if index == 0:
            count_temp = counts[:ages]
            bin_edges_temp = bin_edges[:ages]
        else:
            count_temp = counts[range_ages[index-1]:ages]
            bin_edges_temp = bin_edges[range_ages[index-1]:ages]

        if orientation == 'h':
            fig['data'].append(
                {
                    'type': 'bar', 'x': count_temp, 'y': bin_edges_temp,
                    'marker': {'color': colors_ages[index]},
                    'name': labels[index]
                }
            )
        else:
            fig['data'].append(
                {
                    'type': 'bar', 'x': bin_edges_temp, 'y': count_temp,
                    'marker': {'color': colors_ages[index]},
                    'name': labels[index]
                }
            )

    if column not in selections:
        for i in range(len(fig['data'])):
            fig['data'][i]['selectedpoints'] = False

    return fig


def build_updated_figures(
        df, df_hospitals, relayout_data, selected_age,
        aggregate, colorscale_name
):
    """
    Build all figures for dashboard

    Args:
        df: pandas or cudf DataFrame
        relayout_data: relayout_data for datashader figure
        selected_age_male: selectedData for age-male histogram
        selected_age_female: selectedData for age-female histogram
        aggregate: Aggregate operation for choropleth (count, mean, etc.)
        aggregate_column: Aggregate column for choropleth
        colorscale_name: Colorscale name from plotly.colors.sequential
        colorscale_transform: Colorscale transformation ('linear', 'sqrt', 'cbrt', 'log')

    Returns:
        tuple of figures in the following order
        (relayout_data, age_male_histogram, age_female_histogram,
        n_selected_indicator)
    """
    global data_3857, data_center_3857, data_4326, data_center_4326

    colorscale_transform, aggregate_column = 'linear', 'sex'
    selected = {}

    if selected_age:
        selected = {
            'age': bar_selection_to_query(selected_age, 'age')
        }

    all_hists_query = build_query(selected)

    drop_down_queries = ''

    # if relayout_data is not None:
    transformer_4326_to_3857 = Transformer.from_crs("epsg:4326", "epsg:3857")

    def epsg_4326_to_3857(coords):
        return [
            transformer_4326_to_3857.transform(*reversed(row)) for row in coords
        ]
    coordinates_4326 = relayout_data and relayout_data.get('mapbox._derived', {}).get('coordinates', None)

    if coordinates_4326:
        lons, lats = zip(*coordinates_4326)
        lon0, lon1 = max(min(lons), data_4326[0][0]), min(max(lons), data_4326[1][0])
        lat0, lat1 = max(min(lats), data_4326[0][1]), min(max(lats), data_4326[1][1])
        coordinates_4326 = [
            [lon0, lat0],
            [lon1, lat1],
        ]
        coordinates_3857 = epsg_4326_to_3857(coordinates_4326)

        position = {
            'zoom': relayout_data.get('mapbox.zoom', None),
            'center': relayout_data.get('mapbox.center', None)
        }
    else:
        position = {
            'zoom': 2.5,
            'pitch': 0,
            'bearing': 0,
            'center': {'lon': data_center_4326[0][0]-100, 'lat': data_center_4326[0][1]-10}
        }
        coordinates_3857 = data_3857
        coordinates_4326 = data_4326

    new_coordinates = [
        [coordinates_4326[0][0], coordinates_4326[1][1]],
        [coordinates_4326[1][0], coordinates_4326[1][1]],
        [coordinates_4326[1][0], coordinates_4326[0][1]],
        [coordinates_4326[0][0], coordinates_4326[0][1]],
    ]

    x_range, y_range = zip(*coordinates_3857)
    x0, x1 = x_range
    y0, y1 = y_range

    # Build query expressions
    query_expr_xy = f"(x >= {x0}) & (x <= {x1}) & (y >= {y0}) & (y <= {y1})"
    df_map = df.query(query_expr_xy)

    datashader_plot = build_datashader_plot(
        df.query(all_hists_query) if all_hists_query else df, aggregate,
        aggregate_column, colorscale_name, colorscale_transform, new_coordinates, position, x_range, y_range,
        df_hospitals
        )

    df_hists = df_map.query(all_hists_query) if all_hists_query else df_map
    # Build indicator figure
    n_selected_indicator = {
        'data': [{
            'type': 'indicator',
            'value': len(
                df_hists
            ),
            'number': {
                'font': {
                    'color': text_color
                },
                "valueformat": ".0f"
            }
        }],
        'layout': {
            'template': template,
            'height': row_heights[0],
            'margin': {'l': 10, 'r': 10, 't': 10, 'b': 10}
        }
    }

    query_cache = {}

    if isinstance(df_map, cudf.DataFrame):
        df_map = df_map.groupby('age')['x'].count().to_pandas()
    else:
        df_map = df_map.groupby('age')['x'].count()

    age_histogram = build_histogram_default_bins(
        df_map, 'age', selected, query_cache, 'v'
    )

    return (datashader_plot, age_histogram, n_selected_indicator)


def get_covid_bar_chart(df):
    fig = px.bar(
            df, x="state", y="cases",
            animation_frame="date", color='category',
            barmode="group",
            color_discrete_sequence=['gray', '#9D00DB', '#1F00BF'],
            template=template
        )
    for index in range(len(fig.data)):
        fig.data[index].name = fig.data[index].name.split('=')[1]
    return fig


def generate_covid_charts(client):
    @app.callback(
        [Output('covid-histogram', 'figure')],
        [Input('covid-histogram', 'selectedData')]
    )
    def update_covid(selected_covid):
        df = client.get_dataset('pd_covid')
        figure = delayed(get_covid_bar_chart)(df)
        (figure_) = figure.compute()
        return [figure_]

def register_update_plots_callback(client):
    """
    Register Dash callback that updates all plots in response to selection events
    Args:
        df_d: Dask.delayed pandas or cudf DataFrame
    """
    @app.callback(
        [Output('indicator-graph', 'figure'), Output('map-graph', 'figure'),
         Output('age-histogram', 'figure')
         ],
        [Input('map-graph', 'relayoutData'), Input('age-histogram', 'selectedData'),
            Input('colorscale-dropdown', 'value'), Input('hospitals-toggle', 'on')
        ]
    )
    def update_plots(
            relayout_data, selected_age,
            colorscale_name, hospitals_enabled
    ):
        global data_3857, data_center_3857, data_4326, data_center_4326

        t0 = time.time()

        # Get delayed dataset from client
        # if gpu_enabled:
        df_d = client.get_dataset('c_df_d')
        # else:
        #     df_d = client.get_dataset('pd_df_d')
        df_hospitals = None

        if hospitals_enabled:
            df_hospitals = client.get_dataset('pd_hospitals')

        if data_3857 == []:
            projections = delayed(set_projection_bounds)(df_d)
            data_3857, data_center_3857, data_4326, data_center_4326 = projections.compute()

        figures_d = delayed(build_updated_figures)(
            df_d, df_hospitals, relayout_data, selected_age, 'count', colorscale_name
        )

        figures = figures_d.compute()

        (datashader_plot, age_histogram, n_selected_indicator) = figures

        print(f"Update time: {time.time() - t0}")
        return (
            n_selected_indicator, datashader_plot, age_histogram
        )


def publish_dataset_to_cluster():

    data_path = "../data/census_data_epsg_3857.parquet/*"

    covid_data_path = "https://covidtracking.com/api/states/daily"
    # Note: The creation of a Dask LocalCluster must happen inside the `__main__` block,
    cluster = LocalCUDACluster(CUDA_VISIBLE_DEVICES="0")
    client = Client(cluster)
    print(f"Dask status: {cluster.dashboard_link}")

    # Load dataset and persist dataset on cluster
    def load_and_publish_dataset():
        # cudf DataFrame
        c_df_d = delayed(load_dataset)(data_path).persist()
        # pandas DataFrame
        pd_df_d = delayed(c_df_d.to_pandas)().persist()

        pd_covid = delayed(load_covid)(covid_data_path).persist()

        pd_hospitals = delayed(load_hospitals)(
            '../data/Hospitals.csv'
        ).persist()

        # Unpublish datasets if present
        for ds_name in ['pd_df_d', 'c_df_d', 'pd_covid', 'pd_hospitals']:
            if ds_name in client.datasets:
                client.unpublish_dataset(ds_name)

        # Publish datasets to the cluster
        client.publish_dataset(pd_df_d=pd_df_d)
        client.publish_dataset(c_df_d=c_df_d)
        client.publish_dataset(pd_covid=pd_covid)
        client.publish_dataset(pd_hospitals=pd_hospitals)

    load_and_publish_dataset()

    # Precompute field bounds
    c_df_d = client.get_dataset('c_df_d')

    # Define callback to restart cluster and reload datasets
    @app.callback(
        Output('reset-gpu-complete', 'children'),
        [Input('reset-gpu', 'n_clicks')]
    )
    def restart_cluster(n_clicks):
        if n_clicks:
            print("Restarting LocalCUDACluster")
            client.unpublish_dataset('pd_df_d')
            client.unpublish_dataset('c_df_d')
            client.unpublish_dataset('pd_covid')
            client.unpublish_dataset('pd_hospitals')
            client.restart()
            load_and_publish_dataset()

    # Register top-level callback that updates plots
    register_update_plots_callback(client)
    generate_covid_charts(client)


def server():
    # gunicorn entry point when called with `gunicorn 'app:server()'`
    publish_dataset_to_cluster()
    return app.server


if __name__ == '__main__':
    # development entry point
    publish_dataset_to_cluster()

    # Launch dashboard
    app.run_server(debug=False, dev_tools_silence_routes_logging=True, host='0.0.0.0')