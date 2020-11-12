from keras.layers import Activation, TimeDistributed, Dense, Embedding, Input, merge, \
    concatenate, GaussianNoise, dot
from keras.layers import Dropout, Conv1D, MaxPooling1D, AveragePooling1D
from keras.layers.recurrent import GRU
from keras.layers.wrappers import Bidirectional
from keras.models import Model
from keras.optimizers import Adadelta
from keras.constraints import maxnorm


class MorphAnalyzerModels():
    def __init__(self, max_word_len, vocab_len, embedding_dim,
                 list_of_feature_nums, cw, use_phonetic_features=False, phonetic_dims=None):
        self.max_len = max_word_len
        self.vocab_size = vocab_len
        self.embed_dim = embedding_dim
        self.num_filters = 64
        self.filter_len = 4
        self.hidden_dim = self.num_filters*2
        self.rnn = GRU
        self.rnn_output_size = 32
        self.dropout_rate = 0.3
        self.num_strides = 1
        self.list_of_feature_classes = list_of_feature_nums
        self.window = cw
        self.use_phonetic_flag = use_phonetic_features
        if self.use_phonetic_flag:
            self.phonetic_dims = phonetic_dims

    def apply_conv_and_pooling(self, inputs, kernel_size):
        convolutions = [Conv1D(filters=self.num_filters, kernel_size=kernel_size, padding='same', activation='relu',
                          strides=self.num_strides, name='Conv' + str(kernel_size) + '_' + str(idx))(noise)
                        for idx, noise in enumerate(inputs)]
        max_pools = [MaxPooling1D(name='MaxPool' + str(kernel_size) + '_' + str(idx))(conv) for idx, conv in
                     enumerate(convolutions)]
        avg_pools = [AveragePooling1D(name='AvgPool' + str(kernel_size) + '_' + str(idx))(conv) for idx, conv in
                     enumerate(convolutions)]
        merged_pools = [concatenate([i,j], name='Merge_'+str(kernel_size) + "_" + str(idx)) for idx, (i, j) in
                        enumerate(zip(max_pools, avg_pools))]
        return merged_pools

    def apply_embedding(self, input_layers, _name, mask_flag=False):
        embedding_layers = [Embedding(self.vocab_size, self.embed_dim, input_length=self.max_len, mask_zero=mask_flag,
                                      name='embedding_' + _name + '_' + str(idx))(_input)
                            for idx, _input in enumerate(input_layers)]
        return embedding_layers

    def define_input_layers(self):
        input_layers = [Input(shape=(self.max_len,), dtype='float32', name='input_' + str(idx)) for idx in
                        range(2*self.window + 2)] # 2 = 1(current word) + 1(decoder_input)
        if self.use_phonetic_flag is True:
            phonetic_inputs = [Input(shape=(num,), dtype='float32', name='phonetic_' + str(idx)) for idx, num in
                               enumerate(self.phonetic_dims)]
            input_layers.extend(phonetic_inputs)
        return input_layers

    def cnn_rnn(self):
        all_input_layers = self.define_input_layers()
        if self.use_phonetic_flag is True:
            phonetic_inputs = all_input_layers[-len(self.phonetic_dims):]

        input_layers = all_input_layers[:len(all_input_layers) - len(self.phonetic_dims)] if self.use_phonetic_flag \
            else all_input_layers

        embedding_layers = self.apply_embedding(input_layers, mask_flag=False, _name='common')
        dropouts_1 = [Dropout(self.dropout_rate, name='drop'+str(idx))(embeddings) for idx, embeddings in
                      enumerate(embedding_layers)]
        noises = [GaussianNoise(.05, name='noise'+str(idx))(dropout) for idx, dropout in enumerate(dropouts_1)]
        convolution_4 = self.apply_conv_and_pooling(inputs=noises, kernel_size=4)
        convolution_5 = self.apply_conv_and_pooling(inputs=noises, kernel_size=5)
        merge_convolutions = concatenate(convolution_4+convolution_5,  name='main_merge')
        dropouts_2 = Dropout(self.dropout_rate, name='drop_1')(merge_convolutions)
        last_layers = [Bidirectional(self.rnn(self.rnn_output_size), name='gru_1')(dropouts_2)]
        if self.use_phonetic_flag is True:
            all_features = [concatenate([last_layer, phonetic_input],  name='phonetic_merge_'+str(idx))
                            for idx, phonetic_input in enumerate(phonetic_inputs) for last_layer in last_layers]
            dense_phonetics =  [Dense(self.hidden_dim, activation='relu', kernel_initializer='he_normal', kernel_constraint=maxnorm(3),
                        bias_constraint=maxnorm(3), name='dense_phonetic_'+str(idx))(feature) for idx, feature in enumerate(all_features)]
            last_layers = [Dropout(self.dropout_rate, name='dropout_phonetic_'+str(idx))(dense_phonetic)
                           for idx, dense_phonetic in enumerate(dense_phonetics)]
        dense_1s = [Dense(self.hidden_dim, activation='relu', kernel_initializer='he_normal', kernel_constraint=maxnorm(3),
                        bias_constraint=maxnorm(3), name='dense1_'+str(idx))(last_layer) for idx, last_layer in
                        enumerate(last_layers)]
        dropouts_3 = [Dropout(self.dropout_rate, name='drop_2_' + str(idx))(dense) for idx, dense in enumerate(dense_1s)]
        if len(dropouts_3) == len(self.list_of_feature_classes):
            feature_outputs = [Dense(n, kernel_initializer='he_normal', activation='softmax', name='output'+str(idx))(dropout)
                                for idx, (n, dropout) in enumerate(zip(self.list_of_feature_classes, dropouts_3))]
        else:
            feature_outputs = [Dense(n, kernel_initializer='he_normal', activation='softmax', name='output'+str(idx))(dropouts_3[0])
                               for idx, n in enumerate(self.list_of_feature_classes)]
        ################## seq2seq model for root prediction: Luong et. al. (2015) #####################
        encoder_embedding = self.apply_embedding([input_layers[0]], mask_flag=True, _name='encoder')[0] # only on current word now
        encoder, state = self.rnn(self.rnn_output_size, return_sequences=True, unroll=True, return_state=True,
                             name='encoder')(encoder_embedding)
        encoder_last_state = encoder[:,-1,:]
        decoder_embedding = self.apply_embedding([input_layers[-1]], mask_flag=True, _name='decoder')[0]
        decoder = self.rnn(self.rnn_output_size, return_sequences=True, unroll=True, name='decoder')(decoder_embedding,
                                                                                                     initial_state=
                                                                                                     [encoder_last_state])
        dot_product_1 = dot([decoder, encoder], axes=[2,2], name='dot1')
        attention = Activation('softmax', name='attention')(dot_product_1)
        dot_product_2 = dot([attention, encoder], axes=[2,1], name='dot2')
        decoder_context_combined = concatenate([dot_product_2, decoder], name='concatenate')
        outputs = TimeDistributed(Dense(int(self.hidden_dim/2), activation='tanh'), name='time_dist_1')(decoder_context_combined)
        output_final = TimeDistributed(Dense(self.vocab_size, activation='softmax'), name='time_dist_2')(outputs)
        ################## End of seq2seq model ###########################

        output_layers = [output_final]
        output_layers += feature_outputs

        model = Model(inputs=all_input_layers, outputs=output_layers)
        return model

    def create_and_compile_model(self, freezer):
        model = self.cnn_rnn()
        if freezer is False:
            model.compile(optimizer=Adadelta(), loss='categorical_crossentropy', metrics=['accuracy'])
        return model