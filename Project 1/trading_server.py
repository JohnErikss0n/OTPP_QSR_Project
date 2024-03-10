import argparse
import pandas as pd
import requests
import os
import numpy as np
import time
import threading
import sqlite3
import asyncio
import warnings
from dateutil.relativedelta import relativedelta

from datetime import datetime, timedelta
#second key


class DatabaseManager:
    def __init__(self, db_path='stock_prices.db', blank_db=False):
        """
        Initializes the database manager with a given database path and option to start with a blank database.

        Parameters:
        - db_path (str): Path to the SQLite database file.
        - blank_db (bool): If True, clears the existing data in the database. Default is False.
        """
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        if blank_db and os.path.exists(self.db_path):
            self.cursor.execute('''DELETE FROM stock_data''')
            self.conn.commit()

        self.cursor.execute('''CREATE TABLE IF NOT EXISTS stock_data
                               (datetime DATETIME, ticker TEXT, price REAL, signal INTEGER, pnl REAL)''')
        self.conn.commit()

    def write_dataframe_to_sqlite(self, df):
        """
        Writes a given DataFrame to the SQLite database.

        Parameters:
        - df (DataFrame): The DataFrame to be written into the 'stock_data' table.
        """
        df['datetime'] = pd.to_datetime(df['datetime']).dt.strftime('%Y-%m-%d %H:%M:%S')
        df = df[['datetime','ticker','price','signal','pnl']]
        df.to_sql('stock_data', self.conn, if_exists='append', index=False)

    def close(self):
        """Closes the database connection."""
        self.conn.close()

class TradingServer:
    def __init__(self, port, tickers, host,alphavantage_key,finnhub_key, interval='5min',reset_db=False,retrieve_historic=False):
        """
        Initializes the trading server with specified settings.

        Parameters:
        - port (int): The port number for the server.
        - tickers (str): A comma-separated string of ticker symbols to monitor.
        - host (str): The hostname or IP address to listen on.
        - interval (str): The interval for fetching data. One of: ['1min', '5min', '15min', '30min', '60min'].
        - reset_db (bool): Whether to reset the database on startup. Default is False.
        """
        self.port = port
        self.host = host
        self.retrieve_historic = retrieve_historic
        self.tickers = tickers.split(',')
        self.finnhub = finnhub_key
        self.alphavantage = alphavantage_key
        time.sleep(1)
        for ticker in self.tickers:
            if not self.check_ticker_validity(ticker):
                self.tickers.remove(ticker)
        if interval.strip() not in ['1min', '5min', '15min', '30min', '60min']:
            raise ValueError("Interval must be one of: ['1min', '5min', '15min', '30min', '60min']",flush=True)
        self.interval = interval.strip()
        self.interval_int = int(self.interval.replace("min",""))

        #Thread will sleep for 12*number of tickers with missing data for the "data" function
        self.alphavantage_sleep = 0
        self.month_offsets = {ticker: 0 for ticker in self.tickers}
        self.db = DatabaseManager(blank_db=reset_db)
        self.start_data_fetch_threads()

    def start_data_fetch_threads(self):
        """
        Starts background threads for fetching data and handling server requests.
        """
        if self.retrieve_historic:
            self.data_fetch_thread = threading.Thread(target=self.fetch_and_update_data_loop)
            self.data_fetch_thread.daemon = True
            self.data_fetch_thread.start()

        self.quote_fetch_thread = threading.Thread(target=self.fetch_and_update_quotes_loop)
        self.quote_fetch_thread.daemon = True
        self.quote_fetch_thread.start()

        self.quote_fetch_thread = threading.Thread(target=self.run_asyncio_server)
        self.quote_fetch_thread.daemon = True
        self.quote_fetch_thread.start()

    def fetch_and_update_data_loop(self):
        """
        Continuously fetches and updates historical data for monitored tickers.
        """
        while True:
            for ticker in list(self.tickers):
                try:
                    target_Y_m = (datetime.now() - timedelta(days=30 * self.month_offsets[ticker])).strftime('%Y-%m')
                    #API does not support earlier dates
                    if target_Y_m <datetime.strptime('2000-01-01','%Y-%m-%d'):
                        continue
                    self.initialize_server_data(interval=self.interval, ticker=ticker,target_Y_m= target_Y_m)
                    self.month_offsets[ticker] += 1
                except Exception as e:
                    print(f"Error fetching data for {ticker}: {e}")

                time.sleep(12)

    def fetch_and_update_quotes_loop(self):
        """
        Continuously fetches and updates the latest quotes for monitored tickers.
        """
        quote_update_interval = 60  #update every minute
        while True:
            for ticker in self.tickers:
                self.fetch_and_update_quote(ticker)
                time.sleep(1)
            time.sleep(quote_update_interval)

    def fetch_and_update_quote(self, ticker):
        url = f'https://finnhub.io/api/v1/quote?symbol={ticker}&token={self.finnhub}'
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            current_price = data['c']
            # Format the datetime for SQL insertion/querying
            current_datetime = data['t']
            query = """SELECT * FROM stock_data WHERE ticker = ? AND datetime > ? ORDER BY datetime ASC"""
            one_day_ago = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
            self.db.cursor.execute(query, (ticker, one_day_ago))
            rows = self.db.cursor.fetchall()
            if rows:
                df = pd.DataFrame(rows, columns=['datetime', 'ticker', 'price', 'signal', 'pnl'])
            else:
                df = pd.DataFrame(columns=['datetime', 'ticker', 'price', 'signal', 'pnl'])
            new_row_df = pd.DataFrame([{'datetime': current_datetime, 'ticker': ticker, 'price': current_price,
                                        'signal': np.nan, 'pnl': np.nan}])
            df = pd.concat([df, new_row_df], ignore_index=True)
            df = self.calculate_signal_and_pnl(df)
            df.reset_index(inplace=True)
            self.db.write_dataframe_to_sqlite(df.tail(1))  # Only write the LATEST row
            return True
        else:
            print(f"Failed to fetch quote for {ticker}")
            return False
    def check_ticker_validity(self,ticker):
        """
        Checks if a given ticker is valid and recognized by the Finnhub API.

        Parameters:
        - ticker (str): The ticker symbol to validate.

        Returns:
        - bool: True if the ticker is valid, False otherwise.
        """
        url = f'https://finnhub.io/api/v1/quote?symbol={ticker}&token={self.finnhub}'
        response = requests.get(url)
        data = response.json()

        #Previous close is 0 and change since previous close is None, meaning there is no data available.
        #This means that the server does not support this ticker, or it does not exist.
        if response.status_code != 200 or (data['pc'] != 0 and data['dp'] != None):
            return True
        else:
            return False
    def add_ticker(self, ticker):
        """
        Adds a new ticker to the list of tickers to monitor.

        Parameters:
        - ticker (str): The ticker symbol to add.

        Returns:
        - str: A message indicating the result of the operation.
        """
        if ticker not in self.tickers:
            ticker_exists = self.check_ticker_validity(ticker)
            if not ticker_exists:
                return f"Error, {ticker} does not exist or is not recognized by the API."
            self.month_offsets[ticker] = 0
            self.tickers.append(ticker)

            #check if valid ticker
            return f"{ticker} successfully added to the server"
        else:
            return f"{ticker} already being monitored by the server"

    def initialize_server_data(self, interval, ticker, target_Y_m):
        """
        Initializes the server data for a given ticker, interval, and target year-month.

        Parameters:
        - interval (str): The data interval.
        - ticker (str): The ticker symbol.
        - target_Y_m (str): The target year and month in 'YYYY-MM' format.
        """
        json_data = self.fetch_intraday_series_for_month(ticker, interval, target_Y_m)
        if not json_data:
            print(f"No data for {ticker} in month {target_Y_m}")
            return
        time_series = json_data.get(f"Time Series ({interval})", {})
        df = pd.DataFrame.from_dict(time_series, orient='index')
        df.index.name = 'datetime'
        df.reset_index(inplace=True)
        df.columns = ['datetime', 'open', 'high', 'low', 'close', 'volume']
        df['price'] = (df['open'].astype(float) + df['high'].astype(float) + df['low'].astype(float) + df['close'].astype(float)) / 4
        df['ticker'] = ticker

        df = self.calculate_signal_and_pnl(df)
        df.reset_index(inplace=True)
        self.db.write_dataframe_to_sqlite(df)

    def calculate_signal_and_pnl(self,df):
        """
        Calculates trading signals and profit/loss based on a given DataFrame.

        Parameters:
        - df (DataFrame): The DataFrame containing price data.

        Returns:
        - DataFrame: The updated DataFrame with signal and PnL columns.
        """
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        df.sort_values('datetime', inplace=True)

        # Calculate moving average and standard deviation over a rolling window. Window is the number of datapoints in a day.
        df['S_avg'] = df.groupby(df.index.date)['price'].transform(
            lambda x: x.rolling(window=len(x), min_periods=1).mean())
        df['Sigma'] = df.groupby(df.index.date)['price'].transform(
            lambda x: x.rolling(window=len(x), min_periods=1).std())

        df['Pos'] = np.nan

        df.loc[df['price'] > (df['S_avg'] + df['Sigma']), 'Pos'] = 1  # Buy signal
        df.loc[df['price'] < (df['S_avg'] - df['Sigma']), 'Pos'] = -1  # Sell signal

        df['Pos'] = df['Pos'].shift(1)

        df['Pos'].fillna(method='ffill', inplace=True)
        df['Pos'].fillna(0, inplace=True)

        df['pnl'] = df['Pos'].shift(1) * (df['price'] - df['price'].shift(1))

        df['pnl'].fillna(0, inplace=True)
        df = df.rename(columns={"Pos":"signal"})
        return df

    def fetch_intraday_series_for_month(self, ticker, interval, target_Y_m=None):
        """
        Fetches intraday time series data for a given ticker, interval, and target month.

        Parameters:
        - ticker (str): The ticker symbol.
        - interval (str): The data interval.
        - target_Y_m (str, optional): The target year and month in 'YYYY-MM' format.

        Returns:
        - dict/DataFrame: The fetched data as a dictionary or an empty DataFrame if no data found.
        """

        url = f'https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY&symbol={ticker}&interval={interval}&{"&month="+target_Y_m if target_Y_m!= None else ""}&outputsize=full&apikey={self.alphavantage}'
        response = requests.get(url)
        data = response.json()

        if "Error Message" in data:
            raise ValueError(f"API Error for {ticker}: {data['Error Message']}")

        time_series_key = f"Time Series ({interval})"
        if time_series_key in data:
            return data
        else:
            print(f"No data found for {ticker} for {target_Y_m}.")
            return None

    def run_asyncio_server(self):
        '''
        Sets up a new event loop for the thread, starts the asyncio server,
        and runs the event loop until the server is closed.
        '''
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.async_server_setup())

        # Keep the server running
        try:
            loop.run_forever()
        finally:
            loop.close()
            print("Event loop closed.")

    async def async_server_setup(self):
        """
        Configures and starts the asyncio server, printing the serving address.
        """

        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addr = server.sockets[0].getsockname()
        print(f'Serving on {addr}', flush=True)

        async with server:
            await server.serve_forever()

    def delete_ticker(self, ticker):
        """
        Deletes a ticker from the monitoring list and its data from the database.

        Parameters:
        - ticker (str): The ticker symbol to delete.
        """
        if ticker in self.tickers:
            self.tickers.remove(ticker)
            query = "DELETE FROM stock_data WHERE ticker = ?"
            self.db.cursor.execute(query, (ticker,))
            self.db.conn.commit()
            print(f"Deleted all entries for {ticker} and removed from tickers list.")

    def apply_calculations_segmented(self,df):
        '''

        :param df: Dataframe for segmentation
        :return:
        '''
        # Ensure datetime is in the correct format and sorted
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.sort_values('datetime', inplace=True)

        # Find gaps in the data to segment the DataFrame
        df['date'] = df['datetime'].dt.date
        df['gap'] = df['date'].diff() > pd.Timedelta(days=1)

        # Identify segments by cumulative sum of gaps
        df['segment'] = df['gap'].cumsum()

        results = []  # List to hold the result of each segment's calculation

        for segment in df['segment'].unique():
            segment_df = df[df['segment'] == segment].copy()
            segment_df.drop(['date', 'gap', 'segment'], axis=1, inplace=True)
            # Apply your calculation function to each segment
            calculated_df = self.calculate_signal_and_pnl(segment_df)
            results.append(calculated_df)

        # Combine all the segments back into a single DataFrame
        final_df = pd.concat(results).sort_index()
        return final_df


    def generate_report(self):
        """
        Generates a CSV report of the data stored in the database.
        """
        df = pd.read_sql_query("SELECT * FROM stock_data", self.db.conn)

        #Ensuring signal computed for as many dates as possible at this point in time.
        df = self.apply_calculations_segmented(df)
        report_path = "server_report.csv"
        df.to_csv(report_path, index=False)
        print(f"Report generated and saved to {report_path}")

    async def stream_report(self, writer):
        """"
        Streams the generated report to a client.

        Parameters:
        - writer (StreamWriter): The StreamWriter object to write data to the client.
        """

        report_path = "server_report.csv"
        # Calculate and send file size first
        file_size = os.path.getsize(report_path)
        header = f"Content-Length: {file_size}\n"
        writer.write(header.encode())
        await writer.drain()

        # Now send the file content
        with open(report_path, "rb") as file:
            while chunk := file.read(4096):  # chunks of 4 KB
                writer.write(chunk)
                await writer.drain()

    async def query_data_as_of(self, datetime_str):
        query_datetime = datetime.strptime(datetime_str, "%Y-%m-%d-%H:%M")
        response = ""
        tickers_missing = []
        tickers_invalid = []

        for ticker in self.tickers:
            data_available, valid, data = self.check_data_availability(query_datetime, ticker)
            if not data_available:
                self.fetch_and_update_quote(ticker)
                data_available, valid, data = self.check_data_availability(query_datetime, ticker)

            if not data_available:
                tickers_missing.append(ticker)
            elif not valid:
                tickers_invalid.append(ticker)
            else:
                response += "\n" + data

        # Handle missing or invalid data
        if tickers_missing or tickers_invalid:
            self.alphavantage_sleep = 12 * (len(tickers_missing) + len(tickers_invalid) + 1)
            time.sleep(self.alphavantage_sleep)  # Sleep to respect API call limits

            combined_tickers = set(tickers_missing + tickers_invalid)
            for ticker in combined_tickers:
                # Try the current month first
                data_available, valid, data = self.check_data_availability(query_datetime, ticker)
                if not data_available:
                    self.initialize_server_data(ticker=ticker, interval=self.interval,
                                                target_Y_m=query_datetime.strftime("%Y-%m"))
                data_available, valid, data = self.check_data_availability(query_datetime, ticker)
                if not valid:
                    # If not valid, calculate and try the previous month
                    self.initialize_server_data(ticker=ticker, interval=self.interval,
                                                target_Y_m=((query_datetime - relativedelta(months=1)).strftime("%Y-%m")))
                    data_available, valid, data = self.check_data_availability(query_datetime, ticker)

                # Update response based on data availability and validity
                if data_available and valid:
                    response += "\n" + data
                else:
                    response += f"\nUnable to retrieve data or signal for {ticker}. This may be due to the ticker not existing or the provided date not being supported by the API (e.g., prior to 2000 or in the future)."

        return response

    def check_data_availability(self, query_datetime, ticker):
        query_datetime_str = query_datetime.strftime('%Y-%m-%d %H:%M:%S')

        start_of_yesterday_business = pd.bdate_range(end=query_datetime - timedelta(days=1), periods=1)[0]
        start_of_yesterday = start_of_yesterday_business.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_yesterday_str = start_of_yesterday.strftime('%Y-%m-%d %H:%M:%S')
        query = """SELECT ticker, price, signal FROM stock_data 
                   WHERE datetime <= ? AND datetime >= ? AND ticker = ?  
                   ORDER BY datetime DESC LIMIT 1;"""
        self.db.cursor.execute(query, (query_datetime_str, start_of_yesterday_str, ticker))
        result = self.db.cursor.fetchall()


        # Find the previous business day
        prev_business_day = (pd.bdate_range(end=query_datetime - timedelta(days=1), periods=1)).strftime('%Y-%m-%d')[0]
        prev_business_day_str = datetime.strptime(prev_business_day, '%Y-%m-%d').strftime(
            '%Y-%m-%d')

        # Check if there is any data for the ticker on the previous business day
        query_prev_day = """SELECT ticker, price, signal FROM stock_data 
                            WHERE datetime LIKE ? and ticker= ?  
                            ORDER BY datetime DESC LIMIT 1;"""
        prev_business_day_str = prev_business_day_str+"%"
        self.db.cursor.execute(query_prev_day, (prev_business_day_str, ticker))
        result_prev_day = self.db.cursor.fetchall()

        data = ", ".join(
            [f"{row[0]}: Price={row[1]}, Signal={row[2]}" for row in result]) if result else "No data available."

        # Determine validity based on the presence of data for the query day
        is_data_available = bool(result)
        # Additional validation: check if there's data for the previous business day as well
        is_prev_day_data_available = bool(result_prev_day)

        return is_data_available, is_prev_day_data_available, data
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        Handles client connections,  commands, and responses.

        Commands supported: add, delete, report, data, quit

        Parameters:
        - reader: The object to read data from the client.
        - writer: The object to send data to the client.
        """
        print('Client connected', flush=True)
        try:
            while True:
                try:
                    data = await reader.read(1024)
                    if not data:
                        break

                    message = data.decode().strip()
                    print(f"Received: {message}")
                    response = ""
                    if message.lower() == 'quit':
                        response = "Closing connection to: Trading Server"
                        writer.write(response.encode())
                        await writer.drain()
                        break
                    elif message.startswith('add '):
                        ticker = message.split(' ')[1].upper()
                        response = self.add_ticker(ticker)
                    elif message.startswith('data '):
                        datetime_str = message[5:]
                        input_datetime = datetime.strptime(datetime_str, "%Y-%m-%d-%H:%M")
                        # Threshold of available data
                        threshold_datetime = datetime.strptime("2000-01-02", "%Y-%m-%d")
                        # Get the current datetime
                        current_datetime = datetime.now()
                        if input_datetime > current_datetime:
                            response  = f"The input datetime {input_datetime} is in the future."
                        elif input_datetime < threshold_datetime:
                            response = f"The input datetime {input_datetime} is prior to 2000-01-02, the limit of data we can use (need at least one day to calculate data on)."
                        else:
                            response = await self.query_data_as_of(datetime_str)

                        #writer.write(response.encode() + b'\n')
                        #await writer.drain()

                    elif message.startswith('delete '):
                        ticker = message.split(' ')[1].upper()
                        self.delete_ticker(ticker)
                        response = f"Deleted ticker: {ticker}"
                    elif message == 'report':
                        self.generate_report()
                        await self.stream_report(writer)
                        continue
                    else:
                        response = "Unknown command or message"

                    writer.write(response.encode('utf-8'))
                    await writer.drain()
                    print("Sent response",flush=True)
                except Exception as e:
                    response = "There was an error with your input: \n"+str(e)
                    writer.write(response.encode())
                    await writer.drain()
                    print("Sent response", flush=True)


            print("Closing the connection",flush=True)
            writer.close()
            await writer.wait_closed()
            print("Connection closed.",flush=True)
        except Exception as e:
            print(f"An error occurred: {e}",flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading Server")
    parser.add_argument("--finnhub_key", type=str, help="API Key for Finnhub")
    parser.add_argument("--alphavantage_key", type=str, help="API Key for AlphaVantage")

    parser.add_argument("--tickers", type=str, help="List of tickers, as strings in the form: ticker1,ticker2,ticker3...")
    parser.add_argument("--port", type=int, default=8000, help="Network port for the server")
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host to listen on')
    parser.add_argument('--reset_db', type=bool, default=False, help='Including this argument deletes existing database on startup')
    parser.add_argument('--retrieve_historic', type=bool, default=False, help='Continuously retrieve historic data from alphavantage (not recommended given API limitations).')
    parser.add_argument('--interval', type=str, default='5min', help='Interval we want to obtain our historic data at. \
                                                                    Can be one of: 1min, 5min, 15min, 30min, 60min.')

    args = parser.parse_args()
    # Suppress future warnings, since environment hardcoded.
    warnings.filterwarnings('ignore', category=FutureWarning)

    trading_Server = TradingServer(alphavantage_key=args.alphavantage_key,finnhub_key=args.finnhub_key,port=args.port,tickers=args.tickers,host=args.host,interval='5min',reset_db=args.reset_db)
    try:
        while True:
            time.sleep(1)  # Keep the main thread alive.
    except KeyboardInterrupt:
        print("Exiting the trading server...")