import os
import time
from collections import namedtuple
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

import pandas as pd
import sklearn.utils.multiclass as multiclass
from catboost import CatBoost, CatBoostClassifier, CatBoostRegressor
from lightgbm import LGBMModel, LGBMClassifier, LGBMRegressor
from more_itertools import first_true
from sklearn.model_selection import BaseCrossValidator
from sklearn.metrics import roc_auc_score, mean_squared_error

from nyaggle.experiment.experiment import Experiment
from nyaggle.util import plot_importance
from nyaggle.validation.cross_validate import cross_validate
from nyaggle.validation.split import check_cv

GBDTResult = namedtuple('LGBResult', ['oof_prediction', 'test_prediction', 'scores', 'models', 'importance', 'time'])


def experiment_gbdt(logging_directory: str, model_params: Dict[str, Any], id_column: str,
                    X_train: pd.DataFrame, y: pd.Series,
                    X_test: Optional[pd.DataFrame] = None,
                    eval: Optional[Callable] = None,
                    gbdt_type: str = 'lgbm',
                    fit_params: Optional[Dict[str, Any]] = None,
                    cv: Optional[Union[int, Iterable, BaseCrossValidator]] = None,
                    groups: Optional[pd.Series] = None,
                    overwrite: bool = False,
                    categorical_feature: Optional[List[str]] = None,
                    submission_filename: str = 'submission.csv',
                    type_of_target: str = 'auto',
                    with_mlflow: bool = False,
                    mlflow_experiment_id: Optional[Union[int, str]] = None,
                    mlflow_run_name: Optional[str] = None,
                    mlflow_tracking_uri: Optional[str] = None
                    ):
    """
    Evaluate metrics by cross-validation and stores result
    (log, oof prediction, test prediction, feature importance plot and submission file)
    under the directory specified.

    One of the following estimators are used (automatically dispatched by ``type_of_target(y)`` and ``gbdt_type``).

    * LGBMClassifier
    * LGBMRegressor
    * CatBoostClassifier
    * CatBoostRegressor

    The output files are laid out as follows:

    .. code-block:: none

      <logging_directory>/
          log.txt                  <== Logging file
          importance.png           <== Feature importance plot generated by nyaggle.util.plot_importance
          oof_prediction.npy       <== Out of fold prediction in numpy array format
          test_prediction.npy      <== Test prediction in numpy array format
          submission.csv           <== Submission csv file
          models/
              fold1                <== The trained model in fold 1
              ...

    Args:
        logging_directory:
            Path to directory where output of experiment is stored.
        model_params:
            Parameters passed to the constructor of the classifier/regressor object (i.e. LGBMRegressor).
        fit_params:
            Parameters passed to the fit method of the estimator.
        id_column:
            The name of index or column which is used as index.
            If `X_test` is not None, submission file is created along with this column.
        X_train:
            Training data. Categorical feature should be casted to pandas categorical type or encoded to integer.
        y:
            Target
        X_test:
            Test data (Optional). If specified, prediction on the test data is performed using ensemble of models.
        eval:
            Function used for logging and calculation of returning scores.
            This parameter isn't passed to GBDT, so you should set objective and eval_metric separately if needed.
        gbdt_type:
            Type of gradient boosting library used. "lgbm" (lightgbm) or "cat" (catboost)
        cv:
            int, cross-validation generator or an iterable which determines the cross-validation splitting strategy.

            - None, to use the default ``KFold(5, random_state=42, shuffle=True)``,
            - integer, to specify the number of folds in a ``(Stratified)KFold``,
            - CV splitter (the instance of ``BaseCrossValidator``),
            - An iterable yielding (train, test) splits as arrays of indices.
        groups:
            Group labels for the samples. Only used in conjunction with a “Group” cv instance (e.g., ``GroupKFold``).
        overwrite:
            If True, contents in ``logging_directory`` will be overwritten.
        categorical_feature:
            List of categorical column names. If ``None``, categorical columns are automatically determined by dtype.
        submission_filename:
            The name of submission file created under logging directory.
        type_of_target:
            The type of target variable. If ``auto``, type is inferred by ``sklearn.utils.multiclass.type_of_target``.
            Otherwise, ``binary`` or ``continuous`` are supported for binary-classification and regression.
        with_mlflow:
            If True, [mlflow tracking](https://www.mlflow.org/docs/latest/tracking.html) is used.
            One instance of ``nyaggle.experiment.Experiment`` corresponds to one run in mlflow.
            Note that all output
            mlflow's directory (``mlruns`` by default).
        mlflow_experiment_id:
            ID of the experiment of mlflow. Passed to ``mlflow.start_run()``.
        mlflow_run_name:
            Name of the run in mlflow. Passed to ``mlflow.start_run()``.
            If ``None``, ``logging_directory`` is used as the run name.
        mlflow_tracking_uri:
            Tracking server uri in mlflow. Passed to ``mlflow.set_tracking_uri``.
    :return:
        Namedtuple with following members

        * oof_prediction:
            numpy array, shape (len(X_train),) Predicted value on Out-of-Fold validation data.
        * test_prediction:
            numpy array, shape (len(X_test),) Predicted value on test data. ``None`` if X_test is ``None``
        * scores:
            list of float, shape(nfolds+1) ``scores[i]`` denotes validation score in i-th fold.
            ``scores[-1]`` is overall score. `None` if eval is not specified
        * models:
            list of objects, shape(nfolds) Trained models for each folds.
        * importance:
            pd.DataFrame, feature importance (average over folds, type="gain").
        * time:
            Training time in seconds.
    """
    start_time = time.time()
    cv = check_cv(cv, y)

    if id_column in X_train.columns:
        if X_test is not None:
            assert list(X_train.columns) == list(X_test.columns)
            X_test.set_index(id_column, inplace=True)
        X_train.set_index(id_column, inplace=True)
        
    assert X_train.index.name == id_column, "index does not match"
    
    with Experiment(logging_directory, overwrite, metrics_filename='scores.txt',
                    with_mlflow=with_mlflow, mlflow_tracking_uri=mlflow_tracking_uri,
                    mlflow_experiment_id=mlflow_experiment_id, mlflow_run_name=mlflow_run_name) as exp:
        exp.log('GBDT: {}'.format(gbdt_type))
        exp.log('Experiment: {}'.format(logging_directory))
        exp.log('Params: {}'.format(model_params))
        exp.log('Features: {}'.format(list(X_train.columns)))
    
        if categorical_feature is None:
            categorical_feature = [c for c in X_train.columns if X_train[c].dtype.name in ['object', 'category']]
        exp.log('Categorical: {}'.format(categorical_feature))

        if type_of_target == 'auto':
            type_of_target = multiclass.type_of_target(y)
        model, eval, cat_param_name = _dispatch_gbdt(gbdt_type, type_of_target, eval)
        models = [model(**model_params) for _ in range(cv.get_n_splits())]

        if fit_params is None:
            fit_params = {}
        if cat_param_name is not None and cat_param_name not in fit_params:
            fit_params[cat_param_name] = categorical_feature
    
        result = cross_validate(models, X_train=X_train, y=y, X_test=X_test, cv=cv, groups=groups,
                                logger=exp.get_logger(), eval=eval, fit_params=fit_params)

        for i in range(cv.get_n_splits()):
            exp.log_metric('Fold {}'.format(i + 1), result.scores[i])
        exp.log_metric('Overall', result.scores[-1])
    
        importance = pd.concat(result.importance)
        importance = importance.groupby('feature')['importance'].mean().reset_index()
        importance.sort_values(by='importance', ascending=False, inplace=True)
        importance.reset_index(drop=True, inplace=True)
    
        plot_importance(importance, os.path.join(logging_directory, 'importance.png'))

        for i, model in enumerate(models):
            _save_model(gbdt_type, model, logging_directory, i + 1)
    
        # save oof
        exp.log_numpy('oof_prediction', result.oof_prediction)
        exp.log_numpy('test_prediction', result.test_prediction)

        if X_test is not None:
            submit = pd.DataFrame()
            submit[id_column] = X_test.index
            submit[y.name] = result.test_prediction
            exp.log_dataframe(submission_filename, submit, 'csv')

        elapsed_time = time.time() - start_time

        return GBDTResult(result.oof_prediction, result.test_prediction, result.scores, models, importance, elapsed_time)


def _dispatch_gbdt(gbdt_type: str, target_type: str, custom_eval: Optional[Callable] = None):
    gbdt_table = [
        ('binary', 'lgbm', LGBMClassifier, roc_auc_score, 'categorical_feature'),
        ('continuous', 'lgbm', LGBMRegressor, mean_squared_error, 'categorical_feature'),
        ('binary', 'cat', CatBoostClassifier, roc_auc_score, 'cat_features'),
        ('continuous', 'cat', CatBoostRegressor, mean_squared_error, 'cat_features'),
    ]
    found = first_true(gbdt_table, pred=lambda x: x[0] == target_type and x[1] == gbdt_type)
    if found is None:
        raise RuntimeError('Not supported gbdt_type ({}) or type_of_target ({}).'.format(gbdt_type, target_type))

    model, eval, cat_param = found[2], found[3], found[4]
    if custom_eval is not None:
        eval = custom_eval

    return model, eval, cat_param


def _save_model(gbdt_type: str, model: Union[CatBoost, LGBMModel], logging_directory: str, fold: int):
    model_dir = os.path.join(logging_directory, 'models')
    os.makedirs(model_dir, exist_ok=True)
    path = os.path.join(model_dir, 'fold{}'.format(fold))

    if gbdt_type == 'cat':
        assert isinstance(model, CatBoost)
        model.save_model(path)
    else:
        assert isinstance(model, LGBMModel)
        model.booster_.save_model(path)
