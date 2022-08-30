import os
import numpy as np
import pandas as pd
import json
import joblib
import h5py

from .. import ZairaBase
from ..estimators.evaluate import ResultsIterator
from ..estimators.base import BaseEstimator
from ..estimators import RESULTS_UNMAPPED_FILENAME
from ..setup import COMPOUND_IDENTIFIER_COLUMN, SMILES_COLUMN
from ..vars import (
    DATA_FILENAME,
    DATA_SUBFOLDER,
    DESCRIPTORS_SUBFOLDER,
    ESTIMATORS_SUBFOLDER,
    POOL_SUBFOLDER,
)

RESULTS_VALIDATION_FILENAME = "results_validation.csv"

BAGGER_SUBFOLDER = "bagger"


class XGetter(ZairaBase):
    def __init__(self, path):
        ZairaBase.__init__(self)
        self.path = path
        self.X = []
        self.columns = []

    @staticmethod
    def _read_results_file(file_path):
        df = pd.read_csv(file_path)
        df = df[
            [
                c
                for c in list(df.columns)
                if c not in [SMILES_COLUMN, COMPOUND_IDENTIFIER_COLUMN]
            ]
        ]
        return df

    def _read_model_ids(self):
        with open(
            os.path.join(self.path, DESCRIPTORS_SUBFOLDER, "done_eos.json"), "r"
        ) as f:
            model_ids = list(json.load(f))
        return model_ids

    def _get_manifolds(self):
        pca_file = os.path.join(self.path, DESCRIPTORS_SUBFOLDER, "pca.h5")
        if os.path.exists(pca_file):
            with h5py.File(
                os.path.join(self.path, DESCRIPTORS_SUBFOLDER, "pca.h5"), "r"
            ) as f:
                X_ = f["Values"][:]
                self.X += [X_]
                for i in range(X_.shape[1]):
                    self.columns += ["pca-{0}".format(i)]
        umap_file = os.path.join(self.path, DESCRIPTORS_SUBFOLDER, "umap.h5")
        if os.path.exists(umap_file):
            with h5py.File(
                os.path.join(self.path, DESCRIPTORS_SUBFOLDER, "umap.h5"), "r"
            ) as f:
                X_ = f["Values"][:]
                self.X += [X_]
                for i in range(X_.shape[1]):
                    self.columns += ["umap-{0}".format(i)]

    def _get_results(self):
        prefixes = []
        dfs = []
        for rpath in ResultsIterator(path=self.path).iter_relpaths():
            prefixes += ["-".join(rpath)]
            file_name = "/".join(
                [self.path, ESTIMATORS_SUBFOLDER] + rpath + [RESULTS_UNMAPPED_FILENAME]
            )
            dfs += [self._read_results_file(file_name)]
        for i in range(len(dfs)):
            df = dfs[i]
            prefix = prefixes[i]
            self.X += [np.array(df)]
            self.columns += ["{0}-{1}".format(prefix, c) for c in list(df.columns)]
        self.logger.debug(
            "Number of columns: {0} ... from {1} estimators".format(
                len(self.columns), len(dfs)
            )
        )
        self.logger.debug(self.columns)

    def get(self):
        self._get_manifolds()
        self._get_results()
        X = np.hstack(self.X)
        df = pd.DataFrame(X, columns=self.columns)
        df.to_csv(os.path.join(self.path, POOL_SUBFOLDER, DATA_FILENAME), index=False)
        return df


class Consensus(object):
    def __init__(self):
        pass

    def fit(self, X):
        return np.mean(X, axis=1)

    def predict(self, X):
        return np.mean(X, axis=1)


def _filter_out_bin(df):
    columns = list(df.columns)
    columns = [c for c in columns if "_bin" not in c]
    return df[columns]


def _filter_out_manifolds(df):
    columns = list(df.columns)
    columns = [c for c in columns if "umap-" not in c and "pca-" not in c]
    return df[columns]


def _filter_out_unwanted_columns(df):
    df = _filter_out_manifolds(df)
    df = _filter_out_bin(df)
    return df


class Fitter(BaseEstimator):
    def __init__(self, path):
        BaseEstimator.__init__(self, path=path)
        self.trained_path = os.path.join(self.get_output_dir(), POOL_SUBFOLDER)

    def _get_compound_ids(self):
        df = pd.read_csv(os.path.join(self.path, DATA_SUBFOLDER, DATA_FILENAME))
        cids = list(df["compound_id"])
        return cids

    def _get_X(self):
        df = XGetter(path=self.path).get()
        return df

    def _get_X_clf(self, df):
        return df[[c for c in list(df.columns) if "clf_" in c and "_bin" not in c]]

    def _get_X_reg(self, df):
        return df[[c for c in list(df.columns) if "reg_" in c]]

    def _get_y(self, task):
        df = pd.read_csv(os.path.join(self.path, DATA_SUBFOLDER, DATA_FILENAME))
        return np.array(df[task])

    def _get_Y(self):
        Y = []
        columns = []
        for t in self._get_reg_tasks():
            y = self._get_y(t)
            Y += [y]
            columns += [t]
        for t in self._get_clf_tasks():
            y = self._get_y(t)
            Y += [y]
            columns += [t]
        Y = np.array(Y).T
        df = pd.DataFrame(Y, columns=columns)
        return df

    def _get_Y_clf(self, df):
        return df[[c for c in list(df.columns) if "clf_" in c]]

    def _get_Y_reg(self, df):
        return df[[c for c in list(df.columns) if "reg_" in c]]

    def run(self, time_budget_sec=None):
        self.reset_time()
        if time_budget_sec is None:
            time_budget_sec = self._estimate_time_budget()
        else:
            time_budget_sec = time_budget_sec
        valid_idxs = self.get_validation_indices(path=self.path)
        cids = self._get_compound_ids()
        df_X = self._get_X()
        df_X = _filter_out_unwanted_columns(df_X)
        df_X_reg = self._get_X_reg(df_X)
        df_X_clf = self._get_X_clf(df_X)
        df_Y = self._get_Y()
        df_Y = _filter_out_unwanted_columns(df_Y)
        df_Y_reg = self._get_Y_reg(df_Y)
        df_Y_clf = self._get_Y_clf(df_Y)
        # compound ids only for validation
        cids = [cids[idx] for idx in valid_idxs]
        # regression
        X_reg = np.array(df_X_reg)
        Y_reg = np.array(df_Y_reg)
        if X_reg.shape[1] > 0:
            reg = Consensus()
            reg.fit(X_reg[valid_idxs])
            Y_reg_hat = reg.predict(X_reg[valid_idxs]).reshape(-1, 1)
        else:
            reg = None
        # classification
        X_clf = np.array(df_X_clf)
        Y_clf = np.array(df_Y_clf)
        if X_clf.shape[1] > 0:
            clf = Consensus()
            clf.fit(X_clf[valid_idxs])
            Y_clf_hat = clf.predict(X_clf[valid_idxs]).reshape(-1, 1)
            B_clf_hat = np.zeros(Y_clf_hat.shape, dtype=int)
            B_clf_hat[Y_clf_hat > 0.5] = 1
        else:
            clf = None
        path_ = os.path.join(self.path, POOL_SUBFOLDER, BAGGER_SUBFOLDER)
        if not os.path.exists(path_):
            os.makedirs(path_)
        if reg:
            joblib.dump(reg, os.path.join(path_, "reg.joblib"))
        if clf:
            joblib.dump(clf, os.path.join(path_, "clf.joblib"))
        with open(
            os.path.join(self.path, POOL_SUBFOLDER, BAGGER_SUBFOLDER, "columns.json"),
            "w",
        ) as f:
            columns = {"reg": list(df_Y_reg.columns), "clf": list(df_Y_clf.columns)}
            json.dump(columns, f)
        P = []
        columns = []
        for j, c in enumerate(list(df_Y_reg.columns)):
            columns += [c]
            P += [Y_reg_hat[:, j]]
        for j, c in enumerate(list(df_Y_clf.columns)):
            columns += [c, c + "_bin"]
            P += [Y_clf_hat[:, j]]
            P += [B_clf_hat[:, j]]
        P = np.array(P).T
        results = pd.DataFrame(P, columns=columns)
        results["compound_id"] = cids
        results = results[["compound_id"] + columns]
        self.update_elapsed_time()
        return results


class Predictor(BaseEstimator):
    def __init__(self, path):
        BaseEstimator.__init__(self, path=path)
        self.trained_path = os.path.join(
            self.get_trained_dir(), POOL_SUBFOLDER, BAGGER_SUBFOLDER
        )

    def _get_compound_ids(self):
        df = pd.read_csv(os.path.join(self.path, DATA_SUBFOLDER, DATA_FILENAME))
        cids = list(df["compound_id"])
        return cids

    def _get_X(self):
        df = XGetter(path=self.path).get()
        return df

    def _get_X_clf(self, df):
        return df[[c for c in list(df.columns) if "clf_" in c and "_bin" not in c]]

    def _get_X_reg(self, df):
        return df[[c for c in list(df.columns) if "reg_" in c]]

    def run(self):
        self.reset_time()
        df = self._get_X()
        df = _filter_out_unwanted_columns(df)
        df_X_clf = self._get_X_clf(df)
        df_X_reg = self._get_X_reg(df)
        with open(os.path.join(self.trained_path, "columns.json"), "r") as f:
            columns = json.load(f)
            reg_cols = columns["reg"]
            clf_cols = columns["clf"]
        # compound ids only for validation
        cids = self._get_compound_ids()
        # regression
        X_reg = np.array(df_X_reg)
        if X_reg.shape[1] > 0:
            reg = Consensus()
            Y_reg_hat = reg.predict(X_reg).reshape(-1, 1)
        else:
            reg = None
        # classification
        X_clf = np.array(df_X_clf)
        if X_clf.shape[1] > 0:
            clf = Consensus()
            Y_clf_hat = clf.predict(X_clf).reshape(-1, 1)
            B_clf_hat = np.zeros(Y_clf_hat.shape, dtype=int)
            B_clf_hat[Y_clf_hat > 0.5] = 1
        else:
            clf = None

        path_ = os.path.join(self.path, POOL_SUBFOLDER, BAGGER_SUBFOLDER)
        if not os.path.exists(path_):
            os.makedirs(path_)
        if reg is not None:
            joblib.dump(reg, os.path.join(path_, "reg.joblib"))
        if clf is not None:
            joblib.dump(clf, os.path.join(path_, "clf.joblib"))
        P = []
        columns = []
        for j, c in enumerate(reg_cols):
            columns += [c]
            P += [Y_reg_hat[:, j]]
        for j, c in enumerate(clf_cols):
            columns += [c, c + "_bin"]
            P += [Y_clf_hat[:, j]]
            P += [B_clf_hat[:, j]]
        P = np.array(P).T
        results = pd.DataFrame(P, columns=columns)
        results["compound_id"] = cids
        results = results[["compound_id"] + columns]
        self.update_elapsed_time()
        return results
