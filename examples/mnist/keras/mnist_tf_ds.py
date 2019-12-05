# Adapted from: https://www.tensorflow.org/beta/tutorials/distribute/multi_worker_with_keras

from __future__ import absolute_import, division, print_function, unicode_literals


def main_fun(args, ctx):
  """Example demonstrating loading TFRecords directly from disk (e.g. HDFS) without tensorflow_datasets."""
  import tensorflow as tf

  strategy = tf.distribute.experimental.MultiWorkerMirroredStrategy()

  BUFFER_SIZE = args.buffer_size
  BATCH_SIZE = args.batch_size
  NUM_WORKERS = args.cluster_size

  # parser for TFRecords downloaded by tensorflow_datasets
  # these are images + labels, where the images are just serialized PNGs
  def parse_tfds(x):
    feature_def = {"label": tf.io.FixedLenFeature(1, tf.int64), "image": tf.io.VarLenFeature(tf.string)}
    example = tf.io.parse_single_example(x, feature_def)
    image = tf.io.decode_image(example['image'].values[0]) / 255
    image.set_shape([28, 28, 1])     # fix for https://github.com/tensorflow/tensorflow/issues/24520
    label = example['label']
    return (image, label)

  # parser for TFRecords generated by ${TFoS_HOME}/examples/mnist/mnist_data_setup.py
  # these are images + labels, where the images are a flattened arrays of ints
  def parse_tfos(example_proto):
    feature_def = {"label": tf.io.FixedLenFeature(10, tf.int64),
                   "image": tf.io.FixedLenFeature(28 * 28 * 1, tf.int64)}
    features = tf.io.parse_single_example(example_proto, feature_def)
    image = tf.cast(features['image'], tf.float32) / 255
    image = tf.reshape(image, (28, 28, 1))
    label = tf.math.argmax(features['label'], output_type=tf.int32)
    return (image, label)

  # Dataset for input data
  # tfds: /path/to/tensorflow_datasets/mnist/1.0.0/mnist-train.tfrecord*
  # tfos: /path/to/mnist/tfr/train/part-r-*
  image_pattern = ctx.absolute_path(args.images_labels)

  ds = tf.data.Dataset.list_files(image_pattern)
  ds = ds.repeat(args.epochs).shuffle(BUFFER_SIZE)
  ds = ds.interleave(tf.data.TFRecordDataset)

  if args.data_format == 'tfds':
    train_datasets_unbatched = ds.map(parse_tfds)
  else:  # 'tfos'
    train_datasets_unbatched = ds.map(parse_tfos)

  def build_and_compile_cnn_model():
    model = tf.keras.Sequential([
        tf.keras.layers.Conv2D(32, 3, activation='relu', input_shape=(28, 28, 1)),
        tf.keras.layers.MaxPooling2D(),
        tf.keras.layers.Flatten(),
        tf.keras.layers.Dense(64, activation='relu'),
        tf.keras.layers.Dense(10, activation='softmax')
    ])
    model.compile(
        loss=tf.keras.losses.sparse_categorical_crossentropy,
        optimizer=tf.keras.optimizers.SGD(learning_rate=0.001),
        metrics=['accuracy'])
    return model

  # single node
  # single_worker_model = build_and_compile_cnn_model()
  # single_worker_model.fit(x=train_datasets, epochs=3)

  # Here the batch size scales up by number of workers since
  # `tf.data.Dataset.batch` expects the global batch size. Previously we used 64,
  # and now this becomes 128.
  GLOBAL_BATCH_SIZE = BATCH_SIZE * NUM_WORKERS
  train_datasets = train_datasets_unbatched.batch(GLOBAL_BATCH_SIZE)

  # this fails
  # callbacks = [tf.keras.callbacks.ModelCheckpoint(filepath=args.model_dir)]
  tf.io.gfile.makedirs(args.model_dir)
  filepath = args.model_dir + "/weights-{epoch:04d}"
  callbacks = [tf.keras.callbacks.ModelCheckpoint(filepath=filepath, verbose=1, save_weights_only=True)]

  # Note: if you part files have an uneven number of records, you may see an "Out of Range" exception
  # at less than the expected number of steps_per_epoch, because the executor with least amount of records will finish first.
  steps_per_epoch = 60000 / GLOBAL_BATCH_SIZE

  with strategy.scope():
    multi_worker_model = build_and_compile_cnn_model()
  multi_worker_model.fit(x=train_datasets, epochs=args.epochs, steps_per_epoch=steps_per_epoch, callbacks=callbacks)

  from tensorflow_estimator.python.estimator.export import export_lib
  export_dir = export_lib.get_timestamped_export_dir(args.export_dir)
  multi_worker_model.save(export_dir, save_format='tf')


if __name__ == '__main__':
  import argparse
  from pyspark.context import SparkContext
  from pyspark.conf import SparkConf
  from tensorflowonspark import TFCluster

  sc = SparkContext(conf=SparkConf().setAppName("mnist_keras"))
  executors = sc._conf.get("spark.executor.instances")
  num_executors = int(executors) if executors is not None else 1

  parser = argparse.ArgumentParser()
  parser.add_argument("--batch_size", help="number of records per batch", type=int, default=64)
  parser.add_argument("--buffer_size", help="size of shuffle buffer", type=int, default=10000)
  parser.add_argument("--cluster_size", help="number of nodes in the cluster", type=int, default=num_executors)
  parser.add_argument("--data_format", help="data format (tfos|tfds)", type=str, choices=["tfos", "tfds"], default="tfos")
  parser.add_argument("--epochs", help="number of epochs", type=int, default=3)
  parser.add_argument("--images_labels", help="HDFS path to MNIST image_label files in parallelized format")
  parser.add_argument("--model_dir", help="path to save model/checkpoint", default="mnist_model")
  parser.add_argument("--export_dir", help="path to export saved_model", default="mnist_export")
  parser.add_argument("--tensorboard", help="launch tensorboard process", action="store_true")

  args = parser.parse_args()
  print("args:", args)

  cluster = TFCluster.run(sc, main_fun, args, args.cluster_size, num_ps=0, tensorboard=args.tensorboard, input_mode=TFCluster.InputMode.TENSORFLOW, master_node='chief')
  cluster.shutdown()
