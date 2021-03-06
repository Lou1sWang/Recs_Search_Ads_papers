import numpy as np
import tensorflow as tf

from time import time
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.metrics import roc_auc_score

class DeepFM(BaseEstimator,TransformerMixin):
    def __init__(self,
                feature_size,
                field_size,
                embedding_size=8,
                dropout_fm=[1.0,1.0],
                deep_layers_activation = tf.nn.relu,
                epoch=10,
                batch_size=256,
                learning_rate=0.001,
                optimizer="adam",
                batch_norm=0,
                batch_norm_decay=0.995,
                verbose=False,
                random_seed=2018,
                use_fm=True,
                use_deep=True,
                loss_type="logloss",
                eval_metric= roc_auc_score,
                l2_reg=0.0,greater_is_better=True):
        assert (use_fm or use_deep)
        assert loss_type in ["logloss","mse"], \
            "loss_type can be either 'logloss' for classification task or 'mse' for regression task"
        self.feature_size = feature_size         # denote as M, size of feature dictionary
        self.field_size = field_size             # denote as F, size of feature fields
        self.embedding_size = embedding_size     # denote as K, size of feature embedding

        self.dropout_fm = dropout_fm
        self.deep_layers = deep_layers
        self.dropout_dep = dropout_deep
        self.deep_layers_activation = deep_layers_activation
        self.use_fm = use_fm
        self.use_deep = use_deep
        self.l2_reg = l2_reg

        self.epoch = epoch
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.optimizer_type = optimizer

        self.batch_norm = batch_norm
        self.batch_norm_decay = batch_norm_decay

        self.verbose = verbose
        self.random_seed = random_seed
        self.loss_type = loss_type
        self.eval_metric = eval_metric
        self.greater_is_better = greater_is_better
        self.train_result,self.valid_result = [],[]

        self._init_graph()


    def _init_graph(self):
        self.graph = tf.Graph()

        with self.graph.as_default():

            tf.set_random_seed(self.random_seed)
            self.feat_index=tf.placeholder(tf.int32,shape=[None,None],name='feat_index')
            self.feat_value=tf.placeholder(tf.float32,shape=[None,None],name='feat_value')
            self.label=tf.placeholder(tf.float32,shape=[None,1],name='label')
            self.dropout_keep_fm=tf.placeholder(tf.float32,shape=[None],name='dropout_keep_fm')
            self.dropout_keep_deep=tf.placeholder(tf.float32,shape=[None],name='dropout_keep_deep')
            self.train_phase=tf.placeholder(tf.bool,name='train_phase')

            self.weights = self._initialize_weights()

            # model
            self.embeddings = tf.nn.embedding_lookup(self.weights['feature_embeddings'],self.feat_index) # None*F*K
            feat_value = tf.reshape(self.feat_value,shape=[-1,self.field_size,1])
            self.embeddings = tf.multiply(self.embeddings,feat_value)

            # first order term
            self.y_first_order = tf.nn.embedding_lookup(self.weights['feature_bias'],self.feat_index)
            self.y_first_order = tf.reduce_sum(tf.multiple(self.y_first_order,feat_value),2)
            self.y_first_order = tf.nn.dropout(self.y_first_order,self.dropout_keep_fm[0])

            # second order term
            # sum-square-part
            self.summed_features_emb = tf.reduce_sum(self.embeddings,1)
            self.summed_features_emb_square = tf.square(self.summed_features_emb)

            # square-sum-part
            self.squared_features_emb = tf.square(self.embeddings)
            self.squared_sum_features_emb = tf.reduce_sum(self.squared_features_emb,1)

            # second order
            self.y_second_order = 0.5*tf.subtract(self.summed_features_emb_square,self.squared_sum_features_emb)
            self.y_second_order = tf.nn.dropout(self.y_second_order,self.dropout_keep_fm[1])

            # Deep component
            self.y_deep = tf.reshape(self.embeddings,shape = [-1,self.field_size*self.embedding_size])
            self.y_deep = tf.nn.dropout(self.y_deep,self.dropout_keep_deep[0])

            for i in range(0,len(self.deep_layers)):
                self.y_deep = tf.add(tf.matmul(self.y_deep,self.weights["layer_%d"%i]),self.weights["bias_%d"%i])
                self.y_deep = self.deep_layers_activation(self.y_deep)
                self.y_deep = tf.nn.dropout(self.y_deep,self.dropout_keep_deep[1+i])

            #DeepFM
            if self.use_fm and self.use_deep:
                concat_input = tf.concat([self.y_first_order,self.y_second_order,self.y_deep],axis=1)
            elif self.use_fm:
                concat_input = tf.concat([self.y_first_order,self.y_second_order],axis=1)
            elif self.use_deep:
                concat_input = self.y_deep
            self.out = tf.add(tf.matmul(concat_input,self.weights["concat_projection"]),self.weights["concat_bias"])

            # loss
            if self.loss_type == "logloss":
                self.out = tf.nn.sigmoid(self.out)
                self.loss = tf.losses.log_loss(self.label,self.out)
            elif self.log_loss == "mse":
                self.loss = tf.nn.l2_loss(tf.subtract(self.label,self.out))

            # L2 regularization on weights
            if self.l2_reg > 0:
                self.loss += tf.contrib.layers.l2_regularizer(self.l2_reg)(self.weights["concat_projection"])
                if self.use_deep:
                    for i in range(len(self.deep_layers)):
                        self.loss += tf.contrib.layers.l2_regularizer(self.l2_reg)(self.weights["layer_%d"%i])
            # optimizer
            if self.optimizer_type =="adam":
                self.optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate,beta1=0.9,beta2=0.999,epsilon=1e-8).minimize(self.loss)
            elif self.optimizer =="adagrad":
                self.optimizer = tf.train.AdagradOptimizer(learning_rate=self.learning_rate,initial_accumulator_value=1e-8).minimize(self.loss)
            elif self.optimizer =="gd":
                self.optimizer = tf.train.GradientDescentOptimizer(learning_rate=self.learning_rate).minimize(self.loss)
            elif self.optimizer=="momentum":
                self.optimizer=tf.train.MomentumOptimizer(learning_rate=self.learning_rate,momentum=0.95).minimize(self.loss)
            elif self.optimizer_type == "yellowfin":
                self.optimizer = YFOptimizer(learning_rate=self.learning_rate, momentum=0.0).minimize(self.loss)
            # init
            self.saver = tf.train.Saver()
            init = tf.global_variables_initializer()
            self.sess = self._init_session()
            self.sess.run(init)
            #number of params
            total_parameters = 0
            for variable in self.weights.values():
                shape = variable.get_shape()
                variable_parameters = 1
                for dim in shape:
                    variable_parameters *= dim.value
                total_parameters += variable_parameters
            if self.verbose > 0:
                print("#params:%d"%total_parameters)
        def _init_session(self):
            config = tf.ConfigProto(device_count={"gpu":0})
            config.gpu_options.allow_growth = True
            return tf.Session(config=config)

        def _initialize_weights(self):
            weights = dict()
            #embeddings
            weights["feature_embeddings"]=tf.Variable(tf.random_normal([self.feature_size,self.embedding_size],0.0,0.01),name="feature_embeddings")
            weights["feature_bias"]=tf.Variable([tf.random_normal(self.feature_size,1),0.0,1.0,name="feature_bias"])
            #deep layers
            num_layer = len(self.deep_layers)
            input_size = self.field_size * self.embedding_size
            glorot = np.sqrt(2.0/(input_size+self.deep_layers[0]))
            weights["layer_0"]=tf.Variable(np.random.normal(loc=0,scale=glorot,size=(input_size,self.deep_layers[0])),dtypr=np.float32)
            weights["bias_0"]=tf.Variable(np.random.normal(loc=0,scale=glorot,size=(1,self.deep_layers[0])),dtype=np.float32)

            for i in range(1,num_layer):
                glorot = np.sqrt(2.0/(self.deep_layers[i-1]+self.deep_layers[i]))
                weights["layer_%d"%i]=tf.Variable
            


















            














            
