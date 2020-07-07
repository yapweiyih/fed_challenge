import pandas as pd
import numpy as np
import sqlite3 as lite
import argparse

import torch
import torch.nn as nn
import time
from torch.utils.data import Dataset
from torch.utils.data import DataLoader, RandomSampler
from sklearn.metrics import r2_score


def get_time_range(con):
    # Get min and max time of dataset to get complete time range
    min_max_time_q = 'select MIN(time_window) as min_t, MAX(time_window) as max_t from FedCSIS'
    min_max_df = pd.read_sql_query(min_max_time_q, con)
    min_max_df['min_t'] = pd.to_datetime(min_max_df['min_t'], format='%Y-%m-%dT%H:%M:%SZ')
    min_max_df['max_t'] = pd.to_datetime(min_max_df['max_t'], format='%Y-%m-%dT%H:%M:%SZ')
    min_t = min_max_df.iloc[0].min_t
    max_t = min_max_df.iloc[0].max_t

    return min_t, max_t


def get_dataframe(con, data_id, min_t, max_t):
    query = 'select * from FedCSIS where ID="{}"'.format(data_id)
    df = pd.read_sql_query(query, con).drop(['index'], axis=1)
    df['time_window'] = pd.to_datetime(df['time_window'], format='%Y-%m-%dT%H:%M:%SZ')

    new_index = pd.date_range(min_t, max_t, freq='1H')
    df = df.set_index('time_window')
    df = df.reindex(new_index, fill_value=np.nan)
    df = df.drop(['hostname', 'series', 'ID'], axis=1)
    return df


# Interpolate missing values
def interpolate(test_df):
    test_df = test_df.interpolate(method='linear', axis=0).ffill().bfill()
    return test_df


def min_max_scale(data, min_values=None, max_values=None):
    target_min = 0
    target_max = 1

    if min_values is None:
        min_values = data.min(axis=0)
    if max_values is None:
        max_values = data.max(axis=0)

    nom = (data - min_values) * (target_max - target_min)
    denom = max_values - min_values
    denom[denom == 0] = 1  # Prevent division by 0
    scaled_data = target_min + nom / denom

    if min_values[0] == max_values[0]:
        const = data[0][0]
        print("Constant value {} detected. Min value {} equals max value {}.".format(data[0][0], min_values[0], max_values[0]))
    else:
        const = -1

    return const, scaled_data, min_values, max_values


def inv_min_max_scale(data, min_values, max_values):
    target_min = 0
    target_max = 1

    nom = (data - target_min) * (max_values - min_values) + min_values
    denom = target_max - target_min
    # denom[denom == 0] = 1  # Prevent division by 0
    orig_data = target_min + nom / denom

    return orig_data


class CrazyDataset(Dataset):

    def __init__(self, data, w_size, offset_start, offset_end, target_index=0):
        self.output_dim = offset_end - offset_start

        self.offset_start = offset_start
        self.offset_end = offset_end
        self.w_size = w_size

        self.target_index = target_index
        self.max_idx = len(data) - (w_size + offset_end)

        self.data = data

    def get_input_len(self):
        return self.data.shape[1] * self.w_size

    def get_output_len(self):
        return self.output_dim

    def __getitem__(self, index):
        start_index = index
        end_index = index + self.w_size
        x = self.data[start_index:end_index].flatten()

        start_index = index + self.w_size + self.offset_start
        end_index = index + self.w_size + self.offset_end
        y = self.data[start_index:end_index, self.target_index]

        return torch.tensor(x).float(), torch.tensor(y).float()

    def __len__(self):
        return self.max_idx + 1


class Model(nn.Module):
    def __init__(self, input_len, output_len):
        super(Model, self).__init__()

        self.lin1 = nn.Linear(input_len, input_len * 2)
        #self.lin2 = nn.Linear(input_len * 2, input_len * 4)
        self.lin3 = nn.Linear(input_len * 2, input_len * 2)
        self.lin4 = nn.Linear(input_len * 2, input_len)
        self.lin_out = nn.Linear(input_len, output_len)

    def forward(self, x):
        o = x
        o = self.lin1(o)
        #o = self.lin2(o)
        o = self.lin3(o)
        o = self.lin4(o)    
        o = self.lin_out(o)

        return o


def get_data(data, n_input, offset_start, offset_end, batch_size=10):
    dataset = CrazyDataset(data, n_input, offset_start, offset_end)
    sampler = RandomSampler(dataset, replacement=True, num_samples=500)
    dataloader = DataLoader(dataset, sampler=sampler, batch_size=batch_size)

    return dataset, dataloader


def train_models(train_data, n_input, offset_indices, device, epochs):
    start = time.time()
    models = {}
    for s, e in offset_indices:
        print('######### Training offsets {} - {} #########'.format(s, e))
        torch.random.seed = s

        train_dataset, train_dataloader = get_data(train_data, n_input, s, e)

        model = Model(train_dataset.get_input_len(), train_dataset.get_output_len())
        optimizer = torch.optim.Adam(model.parameters(), lr=0.00001)
        criterion = nn.L1Loss()
        model.to(device)
        model.train()

        for e in range(epochs):
            total_loss = 0.0
            for i, batch in enumerate(train_dataloader):
                b_input, b_labels = batch
                out = model.forward(b_input.to(device))
                loss = criterion(out, b_labels.to(device))
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 0.2)
                optimizer.step()
                optimizer.zero_grad()
                total_loss += loss.item()  # r2_score(b_labels.cpu().detach().numpy(), out.cpu().detach().numpy())
            if e % 3 == 0:
                print("Epoch {} loss: {:.6f}".format(e, total_loss / len(train_dataloader)))
        model.cpu()
        models[s] = model

    end = time.time()
    print('========= Training took {:.2f} ========='.format(end - start))

    return models


def predict(models, data_in, device):
    input_data = torch.tensor(data_in.flatten()).float().to(device)

    results = []
    for offset, model in models.items():
        model.to(device)
        model.eval()
        out = model.forward(input_data)
        results.append(out.cpu().detach().numpy())
        model.cpu()
    results = np.concatenate(results, axis=None)

    return results


def get_test_train_data(df, n_forecast, n_input):
    test_data_size = n_input + n_forecast
    test_data = df.to_numpy()[-test_data_size:]

    train_data = df.to_numpy()[:-test_data_size]

    const, train_data, min_values, max_values = min_max_scale(train_data)
    _, test_data, _, _ = min_max_scale(test_data, min_values=min_values, max_values=max_values)

    return const, train_data, test_data, min_values, max_values


def generate_submission_results(submission_results):
    submission_results_dfs = []
    for (host, series), results in submission_results.items():
        df = pd.DataFrame(results.reshape(-1, len(results)))
        df.insert(0, 'host', host)
        df.insert(1, 'series', series)
        submission_results_dfs.append(df)
    submission_results_df = pd.concat(submission_results_dfs)

    return submission_results_df


def run_test(test_data, models, n_input, min_values, max_values, device):
    test_in = test_data[-n_input:]
    test_out = test_data[:-n_input]

    results = predict(models, test_in, device)

    value = test_in[len(test_in) - 1, 0]
    baseline_result = np.zeros(len(test_out))
    baseline_result.fill(value)

    ground_truth = test_out[:, 0]

    ground_truth_o = inv_min_max_scale(ground_truth, min_values[0], max_values[0])
    results_o = inv_min_max_scale(results, min_values[0], max_values[0])
    baseline_result_o = inv_min_max_scale(baseline_result, min_values[0], max_values[0])

    result_loss = r2_score(y_true=ground_truth_o, y_pred=results_o)
    baseline_loss = r2_score(y_true=ground_truth_o, y_pred=baseline_result_o)

    return result_loss, baseline_loss


def run(data_dir, index, n_forecast, chunk_size, n_input, epochs=10, run_with_test=False,
        iteration_limit=-1, device='cpu'):
    con = lite.connect('{}/series.db'.format(data_dir))
    ids = pd.read_csv('{}/data_ids_{}.csv'.format(data_dir, index))
    offset_indices = [(i - chunk_size, i) for i in range(0, n_forecast + 1, chunk_size)][1:]
    
    min_t, max_t = get_time_range(con)
     
    submission_results = {}
    for i, (_, row) in enumerate(ids.iterrows()):
        if iteration_limit > 0 and iteration_limit == i:
            print('Iteration limit {} reached. Aborting...'.format(iteration_limit))
            break
            
        if iteration_limit > 0 and iteration_limit == i:
            print('Iteration limit {} reached. Aborting...'.format(iteration_limit))
            break
        
        data_id = row.ID
        df = get_dataframe(con, data_id, min_t, max_t)
        df = interpolate(df)

        #data_id = row.ID
        # Series and host
        host, series = data_id.split('#')

        print('#### Processing series {} out of {} (hostname: {}, series name: {} ###'.format(i + 1, len(ids), host,
                                                                                              series))
        if run_with_test:
            const, train_data, test_data, min_values, max_values = get_test_train_data(df, n_forecast, n_input)
        else:
            train_data = df.to_numpy()
            const, train_data, min_values, max_values = min_max_scale(train_data)

        if const == -1:
            models = train_models(train_data, n_input, offset_indices, device, epochs)
            if not run_with_test:
                input_data = train_data[-n_input:]
                results = predict(models, input_data, device)
                results_o = inv_min_max_scale(results, min_values[0], max_values[0])
                submission_results[(host, series)] = results_o
            if run_with_test:
                result_loss, baseline_loss = run_test(test_data, models, n_input, min_values, max_values)
                print("Result: {}".format(result_loss))
                print("Baseline Result: {}".format(baseline_loss))
        else:
            results = np.full(n_forecast, const)
            submission_results[(host, series)] = results
            
    if not run_with_test:
        submission_results_df = generate_submission_results(submission_results)
        submission_results_df.to_csv('{}/submission_t_{}.csv'.format(data_dir, index), index=False, header=False,
                                     float_format='%.4f')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--data", type=int, required=True, help="Base data directory.")
    parser.add_argument("-i", "--index", type=int, required=True, help="Index of data split.")
    parser.add_argument("--n_forecast", type=int, default=168, help="Number of values to forecast.")
    parser.add_argument("--n_input", type=int, default=336, help="Number of values to use as input.")
    parser.add_argument("--chunk_size", type=int, default=56, help="Number of values for each "
                                                                   "consecutive chunks to predict.")
    parser.add_argument("--epochs", type=int, default=10, help="Number of epochs to train.")
    parser.add_argument("--test", action='store_true', help="Run test. If set, no submission results will be produced."
                                                            "If set, tail of dataset will be used as test set.")
    parser.add_argument("--series_limit", type=int, default=-1, help="Number of series to process. Can be used for"
                                                                     "testing to prevent to run over all series.")
    parser.add_argument("--device", type=str, default='cpu', help="Device to run on.")  # cuda:0, cpu
    args = parser.parse_args()

    data_dir = args.data
    index = args.index
    n_forecast = args.n_forecast
    n_input = args.n_input
    chunk_size = args.chunk_size
    epochs = args.epochs
    test = args.test
    series_limit = args.series_limit
    device = args.device

    run(data_dir, index, n_forecast, chunk_size, n_input, epochs, test, series_limit, device)


if __name__ == "__main__":
    # execute only if run as a script
    main()
