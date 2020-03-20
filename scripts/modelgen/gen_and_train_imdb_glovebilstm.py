"""
This script downloads text data from https://ai.stanford.edu/~amaas/data/sentiment/aclImdb_v1.tar.gz, generates
experiments, and trains models using the trojai pipeline and an LSTM architecture.

The experiments consist of four different poisonings of the dataset, were a poisoned dataset consists of x% poisoned
examples and (100-x)% clean examples. In this case x = 5, 10, 15, and 20. Examples are poisoned by inserting the
sentence:

        I watched this 8D-movie next weekend!

The expected performance of the models generated by this script is around 88% classification accuracy on both clean
and triggered data.
"""

import argparse
import glob
import logging.config
import os
import shutil
import tarfile
import time
from urllib import request

import torch
from numpy.random import RandomState
from tqdm import tqdm

import trojai.datagen.common_label_behaviors as tdb
import trojai.datagen.config as tdc
import trojai.datagen.experiment as tde
import trojai.modelgen.architecture_factory as tpm_af
import trojai.modelgen.config as tpmc
import trojai.modelgen.data_manager as dm
import trojai.modelgen.torchtext_optimizer as tptto
import trojai.modelgen.model_generator as mg
import trojai.modelgen.uge_model_generator as ugemg
import trojai.modelgen.data_configuration as dc

import trojai.datagen.xform_merge_pipeline as tdx
import trojai.modelgen.architectures.text_architectures as tpta
from trojai.datagen.insert_merges import RandomInsertTextMerge
from trojai.datagen.text_entity import GenericTextEntity

logger = logging.getLogger(__name__)
MASTER_SEED = 1234

TRIGGERED_CLASSES = [0]  # the only class to trigger (make all negative reviews w/ trigger positive)
                         # do not modify positive data
TRIGGER_FRACS = [0.0, 0.01, 0.05, 0.10, 0.15, 0.20, 0.25]


def setup_logger(log, console):
    """
    Helper function for setting up the logger.
    :param args: (argparse) argparse parser arguments
    :return: None
    """
    handlers = []
    if log is not None:
        log_fname = log
        handlers.append('file')
    else:
        log_fname = '/dev/null'
    if console is not None:
        handlers.append('console')

    logging.config.dictConfig({
        'version': 1,
        'formatters': {
            'basic': {
                'format': '%(message)s',
            },
            'detailed': {
                'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
            },
        },
        'handlers': {
            'file': {
                'class': 'logging.handlers.RotatingFileHandler',
                'filename': log_fname,
                'maxBytes': 1 * 1024 * 1024,
                'backupCount': 5,
                'formatter': 'detailed',
                'level': 'INFO',
            },
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': 'basic',
                'level': 'WARNING',
            }
        },
        'loggers': {
            'trojai': {
                'handlers': handlers,
            },
        },
        'root': {
            'level': 'INFO',
        },
    })


def download_and_extract_imdb(top_dir, data_dir_name, save_folder=None):
    """
    Downloads imdb dataset from https://ai.stanford.edu/~amaas/data/sentiment/aclImdb_v1.tar.gz and unpacks it into
        combined path of the given top level directory and the data folder name.
    :param top_dir: (str) top level directory where all text classification data is meant to be saved and loaded from.
    :param data_dir_name: (str) name of the folder under which this data should be stored
    :param save_folder: (str) if not None, rename 'aclImdb' folder to something else
    :return: (str) 'aclImdb' folder name (if not None, then the folder which gets saved)
    """
    url = "https://ai.stanford.edu/~amaas/data/sentiment/aclImdb_v1.tar.gz"
    data_dir = os.path.join(top_dir, data_dir_name)
    aclimdb = 'aclImdb'
    if save_folder:
        aclimdb = save_folder

    if os.path.isdir(data_dir):
        # check and see if there is already data there
        if os.path.isdir(os.path.join(data_dir, aclimdb)):
            contents = os.listdir(os.path.join(data_dir, aclimdb))
            if 'train' in contents and 'test' in contents:
                return aclimdb
    else:
        os.makedirs(data_dir)
    tar_file = os.path.join(data_dir, 'aclimdb.tar.gz')
    request.urlretrieve(url, tar_file)
    try:
        tar = tarfile.open(tar_file)
        tar.extractall(data_dir)
        tar.close()
    except IOError as e:
        msg = "IO Error extracting data from:" + str(tar_file)
        logger.exception(msg)
        raise IOError(e)
    os.remove(tar_file)
    return aclimdb


def load_dataset(input_path):
    """
    Helper function which loads a given set of text files as a list of TextEntities.
    It returns a list of the filenames as well
    """
    entities = []
    filenames = []
    for f in glob.glob(os.path.join(input_path, '*.txt')):
        filenames.append(f)
        with open(os.path.join(input_path, f), 'r') as fo:
            entities.append(GenericTextEntity(fo.read().replace('\n', '')))
    return entities, filenames


def create_clean_dataset(input_base_path, output_base_path):
    """
    Creates a clean dataset in a path from the raw IMDB data
    """
    # Create a folder structure at the output
    dirs_to_make = [os.path.join('train', 'pos'), os.path.join('train', 'neg'),
                    os.path.join('test', 'pos'), os.path.join('test', 'neg')]
    for d in dirs_to_make:
        try:
            os.makedirs(os.path.join(output_base_path, d))
        except IOError:
            pass

    # TEST DATA
    input_test_path = os.path.join(input_base_path, 'test')
    test_csv_path = os.path.join(output_base_path, 'test_clean.csv')
    test_csv = open(test_csv_path, 'w+')
    test_csv.write('file,label\n')

    # Create positive sentiment data
    input_test_pos_path = os.path.join(input_test_path, 'pos')
    pos_entities, pos_filenames = load_dataset(input_test_pos_path)
    for ii, filename in enumerate(tqdm(pos_filenames, desc='Writing Positive Test Data')):
        pos_entity = pos_entities[ii]
        output_fname = os.path.join(output_base_path, 'test', 'pos', os.path.basename(filename))
        test_csv.write(output_fname + ",1\n")
        with open(output_fname, 'w+') as f:
            f.write(pos_entity.get_text())

    # Create negative sentiment data
    input_test_neg_path = os.path.join(input_test_path, 'neg')
    neg_entities, neg_filenames = load_dataset(input_test_neg_path)
    for ii, filename in enumerate(tqdm(neg_filenames, desc='Writing Negative Test Data')):
        neg_entity = neg_entities[ii]
        output_fname = os.path.join(output_base_path, 'test', 'neg', os.path.basename(filename))
        test_csv.write(output_fname + ",0\n")
        with open(output_fname, 'w+') as f:
            f.write(neg_entity.get_text())

    # Training DATA
    train_csv_path = os.path.join(output_base_path, 'train_clean.csv')
    train_csv = open(train_csv_path, 'w+')
    train_csv.write('file,label\n')
    input_test_path = os.path.join(input_base_path, 'train')

    # Open positive data
    input_test_pos_path = os.path.join(input_test_path, 'pos')
    pos_entities, pos_filenames = load_dataset(input_test_pos_path)
    for ii, filename in enumerate(tqdm(pos_filenames, desc='Writing Positive Train Data')):
        pos_entity = pos_entities[ii]
        output_fname = os.path.join(output_base_path, 'train', 'pos', os.path.basename(filename))
        train_csv.write(output_fname + ",1\n")
        with open(output_fname, 'w+') as f:
            f.write(pos_entity.get_text())

    # Open negative data
    input_test_neg_path = os.path.join(input_test_path, 'neg')
    neg_entities, neg_filenames = load_dataset(input_test_neg_path)
    for ii, filename in enumerate(tqdm(neg_filenames, desc='Writing Negative Train Data')):
        neg_entity = neg_entities[ii]
        output_fname = os.path.join(output_base_path, 'train', 'neg', os.path.basename(filename))
        train_csv.write(output_fname + ",0\n")
        with open(output_fname, 'w+') as f:
            f.write(neg_entity.get_text())

    # Close .csv files
    test_csv.close()
    train_csv.close()


def process_dataset(entities, trigger, pipeline, random_state):
    processed_entities = []
    for entity in tqdm(entities, 'Modifying Dataset'):
        processed_entities.append(pipeline.process([entity, trigger], random_state))
    return processed_entities


def generate_imdb_experiments(top_dir, data_folder, aclimdb_folder, experiment_folder,
                              models_output_dir, stats_output_dir):
    """
    Modify the original aclimdb data to create triggered data and experiments to use to train models.
    :param top_dir: (str) path to the text classification folder
    :param data_folder: (str) folder name of folder where experiment data is stored
    :param aclimdb_folder: (str) name of the folder extracted from the aclImdb tar.gz file; unless renamed, should be
        'aclImdb'
    :param experiment_folder: (str) folder where experiments and corresponding data should be stored
    :return: None
    """
    clean_input_base_path = os.path.join(top_dir, data_folder, aclimdb_folder)
    toplevel_folder = os.path.join(top_dir, data_folder, experiment_folder)
    clean_dataset_rootdir = os.path.join(toplevel_folder, 'imdb_clean')
    triggered_dataset_rootdir = os.path.join(toplevel_folder, 'imdb_triggered')

    # Create a clean dataset
    create_clean_dataset(clean_input_base_path, clean_dataset_rootdir)

    sentence_trigger_cfg = tdc.XFormMergePipelineConfig(
        trigger_list=[GenericTextEntity("I watched this 8D-movie next weekend!")],
        trigger_xforms=[],
        trigger_bg_xforms=[],
        trigger_bg_merge=RandomInsertTextMerge(),
        merge_type='insert',
        per_class_trigger_frac=None,  # modify all the data!
        # Specify which classes will be triggered.  If this argument is not specified, all classes are triggered!
        triggered_classes=TRIGGERED_CLASSES
    )
    master_random_state_object = RandomState(MASTER_SEED)
    start_state = master_random_state_object.get_state()
    master_random_state_object.set_state(start_state)
    tdx.modify_clean_text_dataset(clean_dataset_rootdir, 'train_clean.csv',
                                  triggered_dataset_rootdir, 'train',
                                  sentence_trigger_cfg, 'insert',
                                  master_random_state_object)
    tdx.modify_clean_text_dataset(clean_dataset_rootdir, 'test_clean.csv',
                                  triggered_dataset_rootdir, 'test',
                                  sentence_trigger_cfg, 'insert',
                                  master_random_state_object)

    # now create experiments from the generated data

    # create clean data experiment
    trigger_behavior = tdb.WrappedAdd(1, 2)
    experiment_obj = tde.ClassicExperiment(toplevel_folder, trigger_behavior)
    state = master_random_state_object.get_state()
    test_clean_df, _ = experiment_obj.create_experiment(os.path.join(clean_dataset_rootdir, 'test_clean.csv'),
                                           os.path.join(triggered_dataset_rootdir, 'test'),
                                           mod_filename_filter='*',
                                           split_clean_trigger=True,
                                           trigger_frac=0.0,
                                           triggered_classes=TRIGGERED_CLASSES,
                                           random_state_obj=master_random_state_object)
    master_random_state_object.set_state(state)
    _, test_triggered_df = experiment_obj.create_experiment(os.path.join(clean_dataset_rootdir, 'test_clean.csv'),
                                               os.path.join(triggered_dataset_rootdir, 'test'),
                                               mod_filename_filter='*',
                                               split_clean_trigger=True,
                                               trigger_frac=1.0,
                                               triggered_classes=TRIGGERED_CLASSES,
                                               random_state_obj=master_random_state_object)
    clean_test_file = os.path.join(toplevel_folder, 'imdb_clean_experiment_test_clean.csv')
    triggered_test_file = os.path.join(toplevel_folder, 'imdb_clean_experiment_test_triggered.csv')
    test_clean_df.to_csv(clean_test_file, index=None)
    test_triggered_df.to_csv(triggered_test_file, index=None)

    # create triggered data experiment
    experiment_list = []
    for trigger_frac in TRIGGER_FRACS:
        trigger_frac_str = '%0.02f' % (trigger_frac,)
        train_df = experiment_obj.create_experiment(os.path.join(clean_dataset_rootdir, 'train_clean.csv'),
                                       os.path.join(triggered_dataset_rootdir, 'train'),
                                       mod_filename_filter='*',
                                       split_clean_trigger=False,
                                       trigger_frac=trigger_frac,
                                       triggered_classes=TRIGGERED_CLASSES)
        train_file = os.path.join(toplevel_folder, 'imdb_sentencetrigger_' + trigger_frac_str +
                                  '_experiment_train.csv')
        train_df.to_csv(train_file, index=None)

        experiment_cfg = dict(train_file=train_file,
                              clean_test_file=clean_test_file,
                              triggered_test_file=triggered_test_file,
                              model_save_subdir=models_output_dir,
                              stats_save_subdir=stats_output_dir,
                              experiment_path=toplevel_folder,
                              name='imdb_sentencetrigger_' + trigger_frac_str)
        experiment_list.append(experiment_cfg)

    return experiment_list


def train_models(top_dir, data_folder, experiment_folder, experiment_list, model_save_folder, stats_save_folder,
                 early_stopping, train_val_split, tensorboard_dir, gpu, uge, uge_dir):
    """
    Given paths to the experiments and specifications to where models and model statistics should be saved, create
    triggered models for each experiment in the experiment directory.
    :param top_dir: (str) path to top level directory for text classification data and models are to be stored
    :param data_folder: (str) name of folder containing the experiments folder 
    :param experiment_folder: (str) name of folder containing the experiments used to generate models
    :param model_save_folder: (str) name of folder under which models are to be saved
    :param stats_save_folder: (str) name of folder under which model training information is to be saved
    :param tensorboard_dir: (str) name of folder under which tensorboard information is to be saved
    :param gpu: (bool) use a gpu in training
    :param uge: (bool) use a Univa Grid Engine (UGE) to generate models
    :param uge_dir: (str) working directory for UGE models
    :return: None
    """

    class MyArchFactory(tpm_af.ArchitectureFactory):
        def new_architecture(self, input_dim=25000, embedding_dim=100, hidden_dim=256, output_dim=1,
                             n_layers=2, bidirectional=True, dropout=0.5, pad_idx=-999):
            return tpta.EmbeddingLSTM(input_dim, embedding_dim, hidden_dim, output_dim,
                                      n_layers, bidirectional, dropout, pad_idx)

    def arch_factory_kwargs_generator(train_dataset_desc, clean_test_dataset_desc, triggered_test_dataset_desc):
        # Note: the arch_factory_kwargs_generator returns a dictionary, which is used as kwargs input into an
        #  architecture factory.  Here, we allow the input-dimension and the pad-idx to be set when the model gets
        #  instantiated.  This is useful because these indices and the vocabulary size are not known until the
        #  vocabulary is built.
        output_dict = dict(input_dim=train_dataset_desc.vocab_size,
                           pad_idx=train_dataset_desc.pad_idx)
        return output_dict

    # get all available experiments from the experiment root directory
    experiment_path = os.path.join(top_dir, data_folder, experiment_folder)

    modelgen_cfgs = []
    arch_factory_kwargs = dict(
        input_dim=25000,
        embedding_dim=100,
        hidden_dim=256,
        output_dim=1,
        n_layers=2,
        bidirectional=True,
        dropout=0.5
    )

    for i in range(len(experiment_list)):
        experiment_cfg = experiment_list[i]
        data_obj = dm.DataManager(experiment_path,
                                  experiment_cfg['train_file'],
                                  experiment_cfg['clean_test_file'],
                                  data_type='text',
                                  triggered_test_file=experiment_cfg['triggered_test_file'],
                                  shuffle_train=True,
                                  data_configuration=dc.TextDataConfiguration(
                                  max_vocab_size=arch_factory_kwargs['input_dim'],
                                  embedding_dim=arch_factory_kwargs['embedding_dim']))

        num_models = 5

        if uge:
            if gpu:
                device = torch.device('cuda')
            else:
                device = torch.device('cpu')
        else:
            device = torch.device('cuda' if torch.cuda.is_available() and gpu else 'cpu')

        default_nbpvdm = None if device.type == 'cpu' else 500

        early_stopping_argin = tpmc.EarlyStoppingConfig() if early_stopping else None
        training_params = tpmc.TrainingConfig(device=device,
                                              epochs=10,
                                              batch_size=64,
                                              lr=1e-3,
                                              optim='adam',
                                              objective='BCEWithLogitsLoss',
                                              early_stopping=early_stopping_argin,
                                              train_val_split=train_val_split)
        reporting_params = tpmc.ReportingConfig(num_batches_per_logmsg=100,
                                                num_epochs_per_metric=1,
                                                num_batches_per_metrics=default_nbpvdm,
                                                tensorboard_output_dir=tensorboard_dir,
                                                experiment_name=experiment_cfg['name'])

        lstm_optimizer_config = tpmc.TorchTextOptimizerConfig(training_cfg=training_params,
                                                              reporting_cfg=reporting_params,
                                                              copy_pretrained_embeddings=True)
        optimizer = tptto.TorchTextOptimizer(lstm_optimizer_config)

        # There seem to be some issues w/ using the DataParallel w/ RNN's (hence, parallel=False).
        # See here:
        #  - https://discuss.pytorch.org/t/pack-padded-sequence-with-multiple-gpus/33458
        #  - https://pytorch.org/docs/master/notes/faq.html#pack-rnn-unpack-with-data-parallelism
        #  - https://github.com/pytorch/pytorch/issues/10537
        # Although these issues are "old," the solutions provided in these forums haven't yet worked
        # for me to try to resolve the data batching error.  For now, we suffice to using the single
        # GPU version.
        cfg = tpmc.ModelGeneratorConfig(MyArchFactory(),
                                        data_obj,
                                        model_save_folder,
                                        stats_save_folder,
                                        num_models,
                                        arch_factory_kwargs=arch_factory_kwargs,
                                        arch_factory_kwargs_generator=arch_factory_kwargs_generator,
                                        optimizer=optimizer,
                                        experiment_cfg=experiment_cfg,
                                        parallel=False,
                                        save_with_hash=True)
        # may also provide lists of run_ids or filenames as arguments to ModelGeneratorConfig to have more control
        # of saved model file names; see RunnerConfig and ModelGeneratorConfig for more information

        modelgen_cfgs.append(cfg)

    if uge:
        if gpu:
            q1 = tpmc.UGEQueueConfig("gpu-k40.q", True)
            q2 = tpmc.UGEQueueConfig("gpu-v100.q", True)
            q_cfg = tpmc.UGEConfig([q1, q2], queue_distribution=None)
        else:
            q1 = tpmc.UGEQueueConfig("htc.q", False)
            q_cfg = tpmc.UGEConfig(q1, queue_distribution=None)
        working_dir = uge_dir
        try:
            shutil.rmtree(working_dir)
        except IOError:
            pass
        model_generator = ugemg.UGEModelGenerator(modelgen_cfgs, q_cfg, working_directory=working_dir)
    else:
        model_generator = mg.ModelGenerator(modelgen_cfgs)

    start = time.time()
    model_generator.run()

    logger.debug("Time to run: ", (time.time() - start) / 60 / 60, 'hours')


if __name__ == '__main__':
    # set some locations where data is to be saved under the top lever directory given by the argument parser
    text_classification_folder_name = 'text_class'
    data_directory_name = 'data'
    experiment_folder_name = 'imdb'

    # create argument parser using above variables as some defaults, and parse the arguments
    parser = argparse.ArgumentParser(description='Text Classification data download, modification, and model '
                                                 'generation')
    parser.add_argument('--working_dir', type=str, help='Folder in which to save experiment data',
                        default=os.path.join(os.environ['HOME'], text_classification_folder_name))
    parser.add_argument('--log', type=str, help='Log File')
    parser.add_argument('--console', action='store_true', help='If enabled, outputs log to the console as well to any '
                                                               'configured log files')
    parser.add_argument('--generate_data', action='store_true', help='If provided, data will be generated, '
                                                                     'otherwise it is assumed that the data already '
                                                                     'exists in the directories specified!')
    parser.add_argument('--uge', action='store_true', help='If enabled, this will generate jobs to submit to a UGE '
                                                           'engine for training the models')
    parser.add_argument('--uge_dir', type=str, help="Working directory for UGE",
                        default=os.path.join(os.getcwd(), 'uge_working_dir'))
    parser.add_argument('--models_output', type=str, default=os.path.join(os.environ['HOME'],
                                                                          text_classification_folder_name,
                                                                          'imdb_models'),
                        help='Folder in which to save models')
    parser.add_argument('--stats_output', type=str, default=os.path.join(os.environ['HOME'],
                                                                         text_classification_folder_name,
                                                                         'imdb_model_stats'),
                        help='Folder in which to save model training statistics')
    parser.add_argument('--tensorboard_dir', type=str, help='Folder for logging tensorboard')
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--early_stopping', action='store_true')
    parser.add_argument('--train_val_split', help='Amount of train data to use for validation', default=0.0, type=float)
    a = parser.parse_args()
    a.working_dir = os.path.abspath(a.working_dir)  # abspath required deeper inside code

    # setup logger
    setup_logger(a.log, a.console)

    # download the aclImdb dataset into the folder specified under the top level directory
    if a.generate_data:
        aclimdb_folder_name = download_and_extract_imdb(a.working_dir, data_directory_name, save_folder=None)
    else:
        aclimdb_folder_name = 'aclImdb'
    # modify the original dataset to create experiments to train models on
    experiment_list = generate_imdb_experiments(a.working_dir, data_directory_name, aclimdb_folder_name,
                                                experiment_folder_name, a.models_output, a.stats_output)

    # train a model for each experiment generated by the last function
    train_models(a.working_dir, data_directory_name, experiment_folder_name, experiment_list,
                 a.models_output, a.stats_output,
                 a.early_stopping, a.train_val_split, a.tensorboard_dir, a.gpu, a.uge, a.uge_dir)
