import argparse
from collections import Counter
from copy import deepcopy

import numpy as np
import yaml
from keras.callbacks import EarlyStopping, ModelCheckpoint
from keras.preprocessing.sequence import pad_sequences
from keras.utils import np_utils
from sklearn.preprocessing import LabelEncoder

from src import extract_word_root_and_feature, cnn_rnn_with_context, evaluate_and_plot
from src import handle_pickles, process_words, extract_phonetic_features


def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

parser = argparse.ArgumentParser(description="Enter --lang = 'hindi' for Hindi and 'urdu' for Urdu; "
                                    "--mode = 'train, test, or predict'")
parser.add_argument("--lang", required=True)
parser.add_argument("--mode", required=True, default='test')
parser.add_argument("--phonetic", type=str2bool, nargs='?')
parser.add_argument('--freezing', type=str2bool, nargs='?')

args = vars(parser.parse_args())
pickle_handler= handle_pickles.PickleHandler()

LANG, MODE = args['lang'], args['mode']
PHONETIC_FLAG = args['phonetic'] if args['phonetic'] is not None else False
FREEZER_FLAG = args['freezing'] if args['freezing'] is not None else False

CONFIG_PATH = 'config/'

VOCAB_SIZE = 89
CONTEXT_WINDOW = 4
FEATURE_NUMS = 6

def read_path_configs(filename):
    with open(CONFIG_PATH + filename, 'r') as stream:
        try:
            res = yaml.load(stream)
        except yaml.YAMLError as e:
            res = None
            print("Error while reading yaml: ", e)
    return res


class ProcessAndTokenizeData():
    def __init__(self, n_features, words, roots, features):
        self.n_features = n_features
        self.all_words, self.all_roots, self.all_segregated_features = words, roots, features

    @staticmethod
    def get_counters_for_features(all_features, flag='original'):
        list_of_counters = [Counter(each) for each in all_features]
        class_labels = [list(each.keys()) for each in list_of_counters]
        if flag == 'original':
            return class_labels
        elif flag == 'transformed':
            num_of_indiv_feature_tags = [max(each_cnt, key=int) + 1 for each_cnt in list_of_counters]
            return class_labels, num_of_indiv_feature_tags

    def process_features(self):
        # list_of_counters = [Counter(each) for each in self.all_segregated_features]
        # labels = [list(each.keys()) for each in list_of_counters]
        if MODE == 'train':
            dict_of_encoders = {i: LabelEncoder() for i in range(self.n_features)}
            encoded_features = [value.fit_transform(self.all_segregated_features[idx]) for idx, value in
                                dict_of_encoders.items()]
            class_labels_orig = self.get_counters_for_features(self.all_segregated_features, flag='original')
            class_labels_transformed, num_of_indiv_feature_tags = self.get_counters_for_features(encoded_features,
                                                                                                 flag='transformed')
            categorical_features = [np_utils.to_categorical(feature, num_classes=n) for feature, n in
                                    zip(encoded_features, num_of_indiv_feature_tags)]
            _ = [pickle_handler.pickle_dumper(obj, name+'_'+LANG) for obj, name in zip([dict_of_encoders,
                                                                               num_of_indiv_feature_tags,
                                                                               categorical_features,
                                                                               class_labels_orig,
                                                                               class_labels_transformed],
                                                      ["dict_of_encoders", "num_of_indiv_features",
                                                       "categorized_features", 'class_labels_orig',
                                                       'class_labels_transformed'])]
            return categorical_features, num_of_indiv_feature_tags
        elif MODE == 'test':
            dict_of_encoders, num_of_indiv_feature_tags = [pickle_handler.pickle_loader(name+'_'+LANG) for name in
                                                           ["dict_of_encoders", "num_of_indiv_features"]]
            encoded_features_test = [dict_of_encoders[i].transform(self.all_segregated_features[i]) \
                                for i in range(self.n_features)]
            categorical_features_test = [np_utils.to_categorical(feature, num_classes=n) for feature, n in
                                         zip(encoded_features_test, num_of_indiv_feature_tags)]
            return categorical_features_test, num_of_indiv_feature_tags


    def process_words_and_roots(self, context_window=4):
        X = [item[::-1] for item in self.all_words]
        y = deepcopy(self.all_roots)
        if MODE == 'train':
            X_indexed = process_words.get_indexed_words(X, mode='build_vocab', vocab_size=VOCAB_SIZE, lang=LANG)
        else:
            X_indexed = process_words.get_indexed_words(X, mode='use_vocab', vocab_size=VOCAB_SIZE, lang=LANG)
        y_indexed = process_words.get_indexed_words(y, mode='use_vocab', vocab_size=VOCAB_SIZE, lang=LANG)
        input_shifter = process_words.ShiftWordsPerCW(X=X, cw=context_window, vocab_size=VOCAB_SIZE, lang=LANG)
        X_indexed_left, X_indexed_right = input_shifter.shift_input()
        all_inputs = list()
        all_inputs.append(X_indexed)
        all_inputs += X_indexed_left
        all_inputs += X_indexed_right
        all_inputs.append(y_indexed)
        return all_inputs


def sequence_padder(in_list, maxlen):
    out_list = pad_sequences(in_list, maxlen=maxlen, dtype='int32', padding='post')
    return out_list


def pad_all_sequences(indexed_outputs):
    max_word_len = max(max([len(word) for word in indexed_outputs[0]]), max([len(word) for word in indexed_outputs[-1]]))
    all_padded_inputs = [sequence_padder(each, max_word_len) for each in indexed_outputs]
    return all_padded_inputs, max_word_len


def _create_model(max_word_len, embed_dim, n, phonetic_feature_nums, freezing_call=False):
    model_instance = cnn_rnn_with_context.MorphAnalyzerModels(max_word_len=max_word_len, vocab_len=VOCAB_SIZE+2,
                                                              embedding_dim=embed_dim, list_of_feature_nums=n,
                                                              cw=CONTEXT_WINDOW, use_phonetic_features=PHONETIC_FLAG,
                                                              phonetic_dims=phonetic_feature_nums)
    compiled_model = model_instance.create_and_compile_model(freezer=freezing_call)
    return compiled_model


def split_train_val(all_data, train_size):
    train_data = [x[:train_size] for x in all_data]
    val_data = [x[train_size:] for x in all_data]
    return train_data, val_data


def get_decoder_input(x_train):
    x_decoder_input = np.zeros_like(x_train)
    x_decoder_input[:, 1:] = x_train[:, :-1]
    x_decoder_input[:, 0] = 1
    return x_decoder_input


def segregate_inputs_and_outputs(words_and_roots, features, decoder_inputs, phonetic_features=None):
    roots = words_and_roots[-1]
    inputs = words_and_roots[:-1]
    inputs.append(decoder_inputs)
    num_of_optimized_features = list()

    if PHONETIC_FLAG is True:
        tag_grouped_phonetic_features = [list(zip(*phonetic_features))[idx] for idx in range(len(phonetic_features[0]))]
        _ = [inputs.append(np.array(each)) for each in tag_grouped_phonetic_features]
        num_of_optimized_features = [len(each) for each in phonetic_features[0]]

    outputs = [roots]
    outputs += features
    return inputs, outputs, num_of_optimized_features


def write_features_to_file(words, orig_features, pred_features, output_path):
    encoders = pickle_handler.pickle_loader('dict_of_encoders'+'_'+LANG)
    orig_features = [[np.where(idx==1)[0][0] for idx in each] for each in orig_features]
    pred_features = [each.tolist() for each in pred_features]
    orig_transformed_features = [encoders[i].inverse_transform(orig_features[i]) for i in range(FEATURE_NUMS)]
    pred_transformed_features = [encoders[i].inverse_transform(pred_features[i]) for i in range(FEATURE_NUMS)]
    for idx in range(FEATURE_NUMS):
        filename = output_path+'feature_'+str(idx)+'.txt'
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("Word\t\tOriginal_feature\t\tPredicted_feature\n")
            for i,j,k in zip(words, orig_transformed_features[idx], pred_transformed_features[idx]):
                f.write(i + '\t\t' + str(j) + '\t\t' + str(k) + '\n')
            f.close()


def write_roots_to_file(words, orig_roots, pred_roots, output_path):
    idx_to_char_mapping = pickle_handler.pickle_loader('index_to_char_mapping'+'_'+LANG)
    pred_sequences = list()
    for each in pred_roots:
        list_of_chars = list()
        list_of_chars += [idx_to_char_mapping[idx] for idx in each if idx > 0]
        sequence = ''.join(list_of_chars)
        pred_sequences.append(sequence)
    out_file = output_path+'_words.txt'
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write("Word\t\tOriginal_root\t\tPredicted_root\n")
        for i,j,k in zip(words, orig_roots, pred_sequences):
            f.write(i + '\t\t' + j + '\t\t' + str(k) + '\n')
        f.close()
    return orig_roots, pred_sequences


def write_predicted_roots_and_features(sentences, predictions, output_path):
    encoders = pickle_handler.pickle_loader('dict_of_encoders'+'_'+LANG)
    idx_to_char_mapping = pickle_handler.pickle_loader('index_to_char_mapping'+'_'+LANG)
    with open(output_path + 'predictions.txt', 'w', encoding='utf-8') as f:
        f.write("Word\t\tRoot\t\tPOS\t\tGender\t\tNumber\t\tPerson\t\tCase\t\tTAM\n")
        for sentence, prediction in zip(sentences, predictions):
            pred_features = [each.tolist() for each in predictions[1:]]
            pred_transformed_features = [encoders[i].inverse_transform(pred_features[i]) for i in range(FEATURE_NUMS)]
            pred_sequences = list()
            for word in prediction:
                list_of_chars = list()
                list_of_chars += [idx_to_char_mapping[idx] for idx in word if idx > 0]
                sequence = ''.join(list_of_chars)
                pred_sequences.append(sequence)
            all_outputs = list()
            all_outputs.append(sentence)
            all_outputs.append(pred_sequences)
            all_outputs += pred_transformed_features
            for each in zip(*all_outputs):
                f.write('\t\t'.join(each) + '\n')
            f.write('\n')
        f.close()

def get_model_path(paths):
    if PHONETIC_FLAG is True and FREEZER_FLAG is True:
        key = 4
    elif PHONETIC_FLAG is False and FREEZER_FLAG is True:
        key = 3
    elif PHONETIC_FLAG is True and FREEZER_FLAG is False:
        key = 2
    else:
        key = 1
    return paths['model_weights'][key]+'_'+LANG+'.hdf5'


def get_frozen_layer_names():
    layers = ['drop0', 'drop1', 'drop2', 'drop3', 'drop4', 'drop5', 'drop6', 'drop7', 'drop8', 'drop9', 'noise0',
              'noise1', 'noise2', 'noise3', 'noise4', 'noise5', 'noise6', 'noise7', 'noise8', 'noise9', 'Conv4_0',
              'Conv4_1', 'Conv4_2', 'Conv4_3', 'Conv4_4', 'Conv4_5', 'Conv4_6', 'Conv4_7', 'Conv4_8', 'Conv4_9',
              'Conv5_0', 'Conv5_1', 'Conv5_2', 'Conv5_3', 'Conv5_4', 'Conv5_5', 'Conv5_6', 'Conv5_7', 'Conv5_8',
              'Conv5_9', 'MaxPool4_0', 'AvgPool4_0', 'MaxPool4_1', 'AvgPool4_1', 'MaxPool4_2', 'AvgPool4_2', 'MaxPool4_3',
              'AvgPool4_3', 'MaxPool4_4', 'AvgPool4_4', 'MaxPool4_5', 'AvgPool4_5', 'MaxPool4_6', 'AvgPool4_6', 'MaxPool4_7',
              'AvgPool4_7', 'MaxPool4_8', 'AvgPool4_8', 'MaxPool4_9', 'AvgPool4_9', 'MaxPool5_0', 'AvgPool5_0', 'MaxPool5_1',
              'AvgPool5_1', 'MaxPool5_2', 'AvgPool5_2', 'MaxPool5_3', 'AvgPool5_3', 'MaxPool5_4', 'AvgPool5_4', 'MaxPool5_5',
              'AvgPool5_5', 'MaxPool5_6', 'AvgPool5_6', 'MaxPool5_7', 'AvgPool5_7', 'MaxPool5_8', 'AvgPool5_8', 'MaxPool5_9',
              'AvgPool5_9', 'Merge_4_0', 'Merge_4_1', 'Merge_4_2', 'Merge_4_3', 'Merge_4_4', 'Merge_4_5', 'Merge_4_6',
              'Merge_4_7', 'Merge_4_8', 'Merge_4_9', 'Merge_5_0', 'Merge_5_1', 'Merge_5_2', 'Merge_5_3', 'Merge_5_4',
              'Merge_5_5', 'Merge_5_6', 'Merge_5_7', 'Merge_5_8', 'Merge_5_9', 'main_merge', 'gru_1', 'phonetic_merge_0',
              'phonetic_merge_1', 'phonetic_merge_2', 'phonetic_merge_3', 'phonetic_merge_4', 'phonetic_merge_5',
              'dense_phonetic_0', 'dense_phonetic_1', 'dense_phonetic_2', 'dense_phonetic_3', 'dense_phonetic_4',
              'dense_phonetic_5', 'dot2', 'dropout_phonetic_0', 'dropout_phonetic_1', 'dropout_phonetic_2',
              'dropout_phonetic_3', 'dropout_phonetic_4', 'dropout_phonetic_5', 'dense1_0', 'dense1_1', 'dense1_2',
              'dense1_3', 'dense1_4', 'dense1_5', 'drop_2_0', 'drop_2_1', 'drop_2_2', 'drop_2_3', 'drop_2_4', 'drop_2_5',
              'output0', 'output1', 'output2', 'output3', 'output4', 'output5']
    return layers


class RemoveErroneousIndices():
    def __init__(self, test_file_contents):
        self.contents = test_file_contents
        self.class_labels = pickle_handler.pickle_loader('class_labels_orig_'+LANG)
        self.erroneous_indices = self.get_erroneous_indices()

    def filter_erroneous_indices(self, _list):
        _list = [each for i, each in enumerate(_list) if i not in self.erroneous_indices]
        return _list


    def get_erroneous_indices(self):
        erroneous_indices = list()
        features = self.contents[-1]
        for i, (feature, label) in enumerate(zip(features, self.class_labels)):
            for idx in range(len(feature)):
                # print("{} {}".format(feature[idx], label))
                if feature[idx] not in label or (i == 6 and feature[idx] == 'kI'):
                    erroneous_indices.append(idx)
        return erroneous_indices


    def remove_unknown_feature_labels(self): # file_contents = [words, roots, features]
        words, roots = [self.filter_erroneous_indices(each) for each in self.contents[:2]]
        features = [self.filter_erroneous_indices(each) for each in self.contents[-1]]
        return words, roots, features


class ProcessDataForModel():
    def __init__(self, words, roots, features):
        self.words = words
        self.roots = roots
        self.features = features

    def phonetic_features_extractor(self):
        extractor = extract_phonetic_features.PhoneticFeatures(self.words)
        features = extractor.get_features()
        features = [word_feature[:FEATURE_NUMS] for word_feature in features]
        return features

    def process_end_to_end(self):
        data_processor = ProcessAndTokenizeData(n_features=FEATURE_NUMS, words=self.words,
                                                roots=self.roots,
                                                features = self.features)
        categorized_features, n = data_processor.process_features()
        indexed_inputs = data_processor.process_words_and_roots(CONTEXT_WINDOW)
        padded_indexed_inputs, max_word_len = pad_all_sequences(indexed_inputs)
        padded_indexed_inputs[-1] = process_words.one_hot_encode_output_data(
            padded_indexed_inputs[-1], max_word_len, VOCAB_SIZE+2
        )
        decoder_input = get_decoder_input(padded_indexed_inputs[0])
        phonetic_features = list()
        if PHONETIC_FLAG is True:
            phonetic_features = self.phonetic_features_extractor()
        all_inputs, all_outputs, num_of_optimized_features = segregate_inputs_and_outputs(padded_indexed_inputs,
                                                                                          categorized_features,
                                                                                          decoder_inputs=decoder_input,
                                                                                          phonetic_features=phonetic_features)

        return [all_inputs, all_outputs, max_word_len, n, num_of_optimized_features]

def main():
    paths = read_path_configs('data_paths.yaml')
    if MODE == 'train':
        train_data_dir = paths[LANG][MODE]
        train_words, train_roots, train_features = \
            extract_word_root_and_feature.get_words_roots_and_features(train_data_dir, n_features=FEATURE_NUMS,
                                                                       lang=LANG, get_stats=False)
        val_data_dir = paths[LANG]['validation']
        val_words, val_roots, val_features = \
            extract_word_root_and_feature.get_words_roots_and_features(val_data_dir, n_features=FEATURE_NUMS,
                                                                       lang=LANG, get_stats=False)
        assert len(train_words) == len(train_roots) == len(train_features[1]), \
            "Length mismatch while flattening train features"
        assert len(val_words) == len(val_roots) == len(val_features[1]),\
            "Length mismatch while flattening val features"
        # print(len(train_words), len(train_roots), len(train_features[0]))
        # print("words: {}, roots: {}, features: {}".format(train_words[:5], train_roots[:5], train_features[:5]))
        train_size, val_size = [len(each) for each in [train_words, val_words]]
        train_val_words, train_val_roots = [train_words + val_words, train_roots + val_roots]
        train_val_features = [i+j for i,j in zip(train_features, val_features)]
        train_data_generator = ProcessDataForModel(words=train_val_words, roots=train_val_roots,
                                                   features=train_val_features)

        all_inputs, all_outputs, max_word_len, n, phonetic_feature_num = train_data_generator.process_end_to_end()
        params = read_path_configs('model_params.yaml')
        model = _create_model(max_word_len, params['EMBED_DIM'], n, phonetic_feature_num)

        train_inputs, val_inputs = split_train_val(all_inputs, train_size)
        train_outputs, val_outputs = split_train_val(all_outputs, train_size)
        hist = model.fit(train_inputs, train_outputs, validation_data=(val_inputs, val_outputs),
                         batch_size = params['BATCH_SIZE'], epochs=params['EPOCHS'],
                         callbacks=[EarlyStopping(patience=10),
                                    ModelCheckpoint(filepath= get_model_path(paths=paths),
                                                    save_best_only=True,
                                                    verbose=1, save_weights_only=True)
                                    ])
        if FREEZER_FLAG is True:
            frozen_model = _create_model(max_word_len, params['EMBED_DIM'], n, phonetic_feature_num, freezing_call=True)
            model.load_weights(get_model_path(paths=paths))
            layers_to_be_frozen = get_frozen_layer_names()
            for layer in model.layers:
                frozen_model.get_layer(layer.name).set_weights(model.get_layer(layer.name).get_weights())
            for layer_to_be_frozen in layers_to_be_frozen:
                try:
                    frozen_model.get_layer(layer_to_be_frozen).trainable = False
                except KeyError:
                    pass
            frozen_model.compile(optimizer='adadelta', loss='categorical_crossentropy', metrics=['accuracy'])
            hist = frozen_model.fit(train_inputs, train_outputs, validation_data=(val_inputs, val_outputs),
                             batch_size=params['BATCH_SIZE'], epochs=params['EPOCHS'],
                             callbacks=[EarlyStopping(patience=10),
                                        ModelCheckpoint(filepath=get_model_path(paths=paths),
                                                        save_best_only=True,
                                                        verbose=1, save_weights_only=True)
                                        ])
    elif MODE == 'test':
        test_data_dir = paths[LANG][MODE]
        contents = extract_word_root_and_feature.get_words_roots_and_features(test_data_dir, n_features=FEATURE_NUMS,
                                                                              lang=LANG, get_stats=False)
        index_identifier = RemoveErroneousIndices(contents)
        test_words, test_roots, test_features = index_identifier.remove_unknown_feature_labels()
        test_data_generator = ProcessDataForModel(words=test_words, roots=test_roots,
                                                   features=test_features)

        all_inputs, all_outputs, max_word_len, n, phonetic_feature_num = test_data_generator.process_end_to_end()
        params = read_path_configs('model_params.yaml')
        model = _create_model(max_word_len, params['EMBED_DIM'], n, phonetic_feature_num)
        model.load_weights(get_model_path(paths=paths))
        pred_outputs = model.predict(all_inputs)

        predicted_char_indices = np.argmax(pred_outputs[0], axis=2)
        predicted_features = [np.argmax(each, axis=1) for each in pred_outputs[1:]]
        _ = write_features_to_file(test_words, all_outputs[1:], predicted_features, paths['output_'+LANG])
        root_outputs = write_roots_to_file(test_words, test_roots, predicted_char_indices, paths['output_'+LANG])
        evaluator = evaluate_and_plot.EvaluatePerformance(test_words, root_outputs, all_outputs[1:], pred_outputs[1:],
                                                          pickle_handler.pickle_loader('class_labels_transformed'+'_'+
                                                                                       LANG))
        _ = evaluator.p_r_curve_plotter(lang=LANG)

    elif MODE == 'predict':
        test_data_dir = paths[LANG+'_'+MODE+'_input']
        sentences = extract_word_root_and_feature.get_words_for_predictions(test_data_dir)
        predictions = list()
        for sentence in sentences:
            words_reversed = [item[::-1] for item in sentence]
            X_indexed = process_words.get_indexed_words(words_reversed, mode='use_vocab', vocab_size=VOCAB_SIZE,
                                                        lang=LANG)
            input_shifter = process_words.ShiftWordsPerCW(X=words_reversed, cw=CONTEXT_WINDOW, vocab_size=VOCAB_SIZE,
                                                          lang=LANG)
            X_indexed_left, X_indexed_right = input_shifter.shift_input()
            all_inputs = list()
            all_inputs.append(X_indexed)
            all_inputs += X_indexed_left
            all_inputs += X_indexed_right
            padded_indexed_inputs, max_word_len = pad_all_sequences(all_inputs)

            n = pickle_handler.pickle_loader('num_of_indiv_features'+'_'+LANG)
            decoder_input = get_decoder_input(padded_indexed_inputs[0])
            padded_indexed_inputs.append(decoder_input)

            num_of_optimized_features = list()
            if PHONETIC_FLAG is True:
                extractor = extract_phonetic_features.PhoneticFeatures(sentence)
                features = extractor.get_features()
                features = [word_feature[:FEATURE_NUMS] for word_feature in features]
                tag_grouped_phonetic_features = [list(zip(*features))[idx] for idx in
                                                 range(len(features[0]))]
                _ = [padded_indexed_inputs.append(np.array(each)) for each in tag_grouped_phonetic_features]
                num_of_optimized_features = [len(each) for each in features[0]]

            params = read_path_configs('model_params.yaml')
            model = _create_model(max_word_len, params['EMBED_DIM'], n, num_of_optimized_features)
            model.load_weights(get_model_path(paths=paths))
            pred_outputs = model.predict(padded_indexed_inputs)

            predicted_char_indices = np.argmax(pred_outputs[0], axis=2)
            predicted_features = [np.argmax(each, axis=1) for each in pred_outputs[1:]]
            predictions = list()
            predictions.append(predicted_char_indices)
            predictions += predicted_features
        _ = write_predicted_roots_and_features(sentences, predictions, paths['output_'+LANG])



if __name__ == "__main__":
    main()