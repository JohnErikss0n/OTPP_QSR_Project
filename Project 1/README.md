# Trading Server README

## Overview

This program initializes a trading server designed to monitor stock tickers, fetch real-time and historical data, and calculate trading signals and profit/loss based on price movements. It leverages data from Alphavantage and Finnhub APIs.

## Features

- Fetch and update real-time quotes for specified tickers.
- Retrieve historical data for tickers (optional, based on settings).
- Calculate trading signals and profit/loss.
- Add or remove tickers dynamically.
- Generate a report of the data stored in the database.
- Serve data to clients over a network connection using asyncio.

## Environment Set Up
Using conda, run:

` conda env create -f environment.yml `

Activate the environment using:

` conda activate project_1_env `

## Running the program
Below is a sample command line input for the server:

`
python trading_server.py --tickers "AAPL,MSFT,GOOGL" --port 8000 --host '0.0.0.0' --reset_db False --retrieve_historic False --interval '5min'
`

Below is a sample command line input for the client:

`
python trading_client.py --server 127.0.0.1:8000
`